from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ..db import Database

PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 12 * 60 * 60
USERNAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{2,31}$")
SESSION_COOKIE = "musiclib_session"


def valid_username(username: str) -> bool:
    return bool(USERNAME_PATTERN.fullmatch(username))


def valid_password(password: str) -> bool:
    if not 12 <= len(password) <= 128 or "\x00" in password:
        return False
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join((
        "pbkdf2_sha256", str(PASSWORD_ITERATIONS),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    ))


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_hash = stored.split("$", 3)
        iterations = int(raw_iterations)
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_hash.encode("ascii"))
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 1_000_000:
            return False
    except (ValueError, TypeError):
        return False
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", encoded, salt, iterations)
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def verify_csrf(token: str, stored_hash: str) -> bool:
    try:
        actual = csrf_hash(token)
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(actual, stored_hash)


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    def login(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.db.get_user_by_username(username.strip())
        stored = user["password_hash"] if user else self._dummy_hash
        valid = verify_password(password, stored)
        if user is None or not user["active"] or not valid:
            return None
        return user

    @property
    def _dummy_hash(self) -> str:
        # A fixed, valid hash keeps unknown-user login attempts on the same path.
        return (
            "pbkdf2_sha256$600000$"
            "AAAAAAAAAAAAAAAAAAAAAA==$"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        )

    def create_session(self, user_id: int) -> Tuple[str, str, float]:
        token = secrets.token_urlsafe(32)
        csrf = self.csrf_token(token)
        expires_at = time.time() + SESSION_SECONDS
        self.db.create_auth_session(token_hash(token), user_id, csrf_hash(csrf), expires_at)
        return token, csrf, expires_at

    @staticmethod
    def csrf_token(token: str) -> str:
        return hmac.new(token.encode("ascii"), b"musiclib-csrf", hashlib.sha256).hexdigest()

    def session(self, token: str) -> Optional[Dict[str, Any]]:
        if len(token) > 128:
            return None
        try:
            return self.db.get_auth_session(token_hash(token))
        except UnicodeEncodeError:
            return None

    def revoke_session(self, token: str) -> None:
        self.db.delete_auth_session(token_hash(token))


class LoginLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 300, block_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self._lock = threading.Lock()
        self._attempts: Dict[str, Tuple[int, float, float]] = {}

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            record = self._attempts.get(key)
            if record and record[2] > now:
                return max(1, int(record[2] - now))
            return 0

    def failed(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts, started, blocked_until = self._attempts.get(key, (0, now, 0.0))
            if now - started >= self.window_seconds:
                attempts, started = 0, now
            attempts += 1
            blocked_until = now + self.block_seconds if attempts >= self.limit else 0.0
            self._attempts[key] = (attempts, started, blocked_until)
            if len(self._attempts) > 10_000:
                self._attempts = {
                    item: value for item, value in self._attempts.items()
                    if value[1] + self.window_seconds > now or value[2] > now
                }

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
