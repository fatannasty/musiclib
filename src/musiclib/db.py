from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .models import Album, Issue, Track


def music_search_clause(query: str) -> tuple[str, List[str]]:
    clauses, params = [], []
    for word in query.split():
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(" + " OR ".join(
            f"{column} LIKE ? ESCAPE '\\' COLLATE NOCASE"
            for column in ("title", "artist", "album_artist", "album")
        ) + ")")
        params.extend([pattern] * 4)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    mtime REAL NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    format TEXT,
    bitrate INTEGER,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER,
    duration REAL,
    artist TEXT,
    album_artist TEXT,
    album TEXT,
    title TEXT,
    track_no INTEGER,
    track_total INTEGER,
    disc_no INTEGER,
    disc_total INTEGER,
    year INTEGER,
    genre TEXT,
    mbid_track TEXT,
    mbid_release TEXT,
    has_embedded_art INTEGER NOT NULL DEFAULT 0,
    file_hash TEXT,
    fingerprint TEXT,
    quality_score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_artist, album);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(artist, album, title);
CREATE INDEX IF NOT EXISTS idx_tracks_hash ON tracks(file_hash);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    detail TEXT,
    severity TEXT NOT NULL DEFAULT 'warn',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_track ON issues(track_id);

CREATE TABLE IF NOT EXISTS dupe_groups (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,
    keep_id INTEGER REFERENCES tracks(id),
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dupe_members (
    group_id INTEGER NOT NULL REFERENCES dupe_groups(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, track_id)
);

CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album_artist TEXT NOT NULL,
    album TEXT NOT NULL,
    year INTEGER,
    mbid_release TEXT,
    mbid_verified INTEGER NOT NULL DEFAULT 0,
    art_status TEXT NOT NULL DEFAULT 'unknown',
    expected_tracks INTEGER,
    have_tracks INTEGER NOT NULL DEFAULT 0,
    missing_tracks TEXT,
    UNIQUE(album_artist, album)
);

CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER,
    file_hash TEXT,
    reason TEXT,
    moved_to TEXT,
    deleted_at TEXT NOT NULL,
    restored_at TEXT
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    nzo_id TEXT,
    title TEXT,
    url TEXT,
    indexer TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    requested_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_admin
    ON users(role) WHERE role='admin';
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_order
    ON playlist_tracks(playlist_id, position, id);

CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_recent ON favorites(user_id, created_at DESC, track_id);
"""

TRACK_COLUMNS = [
    "id", "path", "mtime", "size", "format", "bitrate", "sample_rate", "bit_depth",
    "channels", "duration", "artist", "album_artist", "album", "title", "track_no",
    "track_total", "disc_no", "disc_total", "year", "genre", "mbid_track",
    "mbid_release", "has_embedded_art", "file_hash", "fingerprint", "quality_score",
    "status", "scanned_at",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self.albums_dirty = True
        if "evidence" not in {
            row["name"] for row in self._conn.execute("PRAGMA table_info(dupe_groups)")
        }:
            self._conn.execute("ALTER TABLE dupe_groups ADD COLUMN evidence TEXT")
        deletion_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(deletions)")
        }
        if "restored_at" not in deletion_columns:
            self._conn.execute("ALTER TABLE deletions ADD COLUMN restored_at TEXT")
        album_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(albums)")
        }
        for column in ("track_signature", "release_signature"):
            if column not in album_columns:
                self._conn.execute(f"ALTER TABLE albums ADD COLUMN {column} TEXT")
        playlist_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(playlists)")
        }
        if "owner_id" not in playlist_columns:
            self._conn.execute(
                "ALTER TABLE playlists ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_playlists_owner ON playlists(owner_id)"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def upsert_track(self, track: Track) -> Track:
        values = [
            track.path, track.mtime, track.size, track.format, track.bitrate,
            track.sample_rate, track.bit_depth, track.channels, track.duration,
            track.artist, track.album_artist, track.album, track.title,
            track.track_no, track.track_total, track.disc_no, track.disc_total,
            track.year, track.genre, track.mbid_track, track.mbid_release,
            int(track.has_embedded_art), track.file_hash, track.fingerprint,
            track.quality_score, track.status, utcnow(),
        ]
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tracks (path, mtime, size, format, bitrate, sample_rate,
                    bit_depth, channels, duration, artist, album_artist, album, title,
                    track_no, track_total, disc_no, disc_total, year, genre,
                    mbid_track, mbid_release, has_embedded_art, file_hash,
                    fingerprint, quality_score, status, scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    mtime=excluded.mtime, size=excluded.size, format=excluded.format,
                    bitrate=excluded.bitrate, sample_rate=excluded.sample_rate,
                    bit_depth=excluded.bit_depth, channels=excluded.channels,
                    duration=excluded.duration, artist=excluded.artist,
                    album_artist=excluded.album_artist, album=excluded.album,
                    title=excluded.title, track_no=excluded.track_no,
                    track_total=excluded.track_total, disc_no=excluded.disc_no,
                    disc_total=excluded.disc_total, year=excluded.year,
                    genre=excluded.genre, mbid_track=excluded.mbid_track,
                    mbid_release=excluded.mbid_release,
                    has_embedded_art=excluded.has_embedded_art,
                    file_hash=excluded.file_hash, fingerprint=excluded.fingerprint,
                    quality_score=excluded.quality_score, status=excluded.status,
                    scanned_at=excluded.scanned_at
                """,
                values,
            )
            row = self._conn.execute(
                "SELECT id FROM tracks WHERE path=?", (track.path,)
            ).fetchone()
            self._conn.commit()
            self.albums_dirty = True
            if row:
                track.id = row["id"]
            elif cur.lastrowid:
                track.id = cur.lastrowid
        self.replace_issues(track)
        return track

    def update_track_columns(self, track_id: int, **columns: Any) -> None:
        if not columns:
            return
        cols = ", ".join(f"{k}=?" for k in columns)
        params = list(columns.values()) + [track_id]
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE tracks SET {cols} WHERE id=?", params)
            if set(columns) & {
                "album_artist", "album", "year", "mbid_release", "status",
                "title", "track_no", "disc_no", "path", "has_embedded_art",
            }:
                self.albums_dirty = True

    def replace_issues(self, track: Track) -> None:
        self.execute("DELETE FROM issues WHERE track_id=?", (track.id,))
        if track.issues:
            self.executemany(
                "INSERT INTO issues (track_id, kind, detail, severity, created_at) "
                "VALUES (?,?,?,?,?)",
                [(track.id, i.kind, i.detail, i.severity, utcnow()) for i in track.issues],
            )

    def get_track(self, track_id: int) -> Optional[Track]:
        row = self.query_one("SELECT * FROM tracks WHERE id=?", (track_id,))
        return self._row_to_track(row) if row else None

    def audio_format_counts(self) -> List[Dict[str, Any]]:
        return [
            dict(row) for row in self.query(
                "SELECT COALESCE(NULLIF(format, ''), 'unknown') AS format, COUNT(*) AS tracks "
                "FROM tracks WHERE status IN ('ok', 'suspect', 'bad') "
                "GROUP BY 1 ORDER BY 1 COLLATE NOCASE"
            )
        ]

    def create_user(self, username: str, password_hash: str, role: str = "user") -> int:
        if role not in ("admin", "user"):
            raise ValueError("Invalid user role.")
        now = utcnow()
        with self._lock, self._conn:
            count = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            if role == "admin" and count:
                raise ValueError("An administrator already exists.")
            if role == "user" and not count:
                raise ValueError("Create the first administrator before adding users.")
            cursor = self._conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (username, password_hash, role, now),
            )
            user_id = cursor.lastrowid
            if role == "admin":
                self._conn.execute(
                    "UPDATE playlists SET owner_id=? WHERE owner_id IS NULL", (user_id,)
                )
            if user_id is None:
                raise sqlite3.DatabaseError("User creation did not return an ID.")
            return user_id

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM users WHERE username=?", (username,))
        return dict(row) if row else None

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.query_one(
            "SELECT id, username, role, active, created_at FROM users WHERE id=?",
            (user_id,),
        )
        return dict(row) if row else None

    def users(self) -> List[Dict[str, Any]]:
        return [
            dict(row) for row in self.query(
                "SELECT id, username, role, active, created_at FROM users ORDER BY id"
            )
        ]

    def user_count(self) -> int:
        return int(self.query_one("SELECT COUNT(*) AS n FROM users")["n"])

    def create_auth_session(
        self, session_hash: str, user_id: int, csrf_hash: str, expires_at: float,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (time.time(),))
            self._conn.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, csrf_hash, expires_at, created_at) "
                "VALUES (?,?,?,?,?)",
                (session_hash, user_id, csrf_hash, expires_at, utcnow()),
            )

    def get_auth_session(self, session_hash: str) -> Optional[Dict[str, Any]]:
        row = self.query_one(
            "SELECT s.token_hash, s.csrf_hash, s.expires_at, u.id, u.username, u.role "
            "FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
            (session_hash, time.time()),
        )
        return dict(row) if row else None

    def delete_auth_session(self, session_hash: str) -> None:
        self.execute("DELETE FROM auth_sessions WHERE token_hash=?", (session_hash,))

    def delete_user_sessions(self, user_id: int, except_hash: Optional[str] = None) -> None:
        if except_hash is None:
            self.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        else:
            self.execute(
                "DELETE FROM auth_sessions WHERE user_id=? AND token_hash!=?",
                (user_id, except_hash),
            )

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        self.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))

    def delete_user(self, user_id: int) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None or row["role"] == "admin":
                return False
            cursor = self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            return cursor.rowcount > 0

    def playlists(self, owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
        where = "" if owner_id is None else "WHERE p.owner_id=?"
        params = () if owner_id is None else (owner_id,)
        return [
            dict(row) for row in self.query(
                "SELECT p.id, p.name, p.created_at, p.updated_at, COUNT(pt.id) AS track_count "
                "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id=p.id "
                f"{where} GROUP BY p.id ORDER BY p.name COLLATE NOCASE, p.id",
                params,
            )
        ]

    def create_playlist(self, name: str, owner_id: Optional[int] = None) -> int:
        now = utcnow()
        cursor = self.execute(
            "INSERT INTO playlists (owner_id, name, created_at, updated_at) VALUES (?,?,?,?)",
            (owner_id, name, now, now),
        )
        if cursor.lastrowid is None:
            raise sqlite3.DatabaseError("Playlist creation did not return an ID.")
        return cursor.lastrowid

    def rename_playlist(
        self, playlist_id: int, name: str, owner_id: Optional[int] = None,
    ) -> bool:
        owner_filter = "" if owner_id is None else " AND owner_id=?"
        params = (name, utcnow(), playlist_id) if owner_id is None else (
            name, utcnow(), playlist_id, owner_id
        )
        cursor = self.execute(
            f"UPDATE playlists SET name=?, updated_at=? WHERE id=?{owner_filter}",
            params,
        )
        return cursor.rowcount > 0

    def delete_playlist(self, playlist_id: int, owner_id: Optional[int] = None) -> bool:
        owner_filter = "" if owner_id is None else " AND owner_id=?"
        params = (playlist_id,) if owner_id is None else (playlist_id, owner_id)
        cursor = self.execute(
            f"DELETE FROM playlists WHERE id=?{owner_filter}", params
        )
        return cursor.rowcount > 0

    def playlist(
        self, playlist_id: int, owner_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        owner_filter = "" if owner_id is None else " AND owner_id=?"
        params = (playlist_id,) if owner_id is None else (playlist_id, owner_id)
        row = self.query_one(
            f"SELECT id, name, created_at, updated_at FROM playlists WHERE id=?{owner_filter}",
            params,
        )
        if row is None:
            return None
        tracks = self.query(
            "SELECT pt.id AS entry_id, pt.position, t.id, t.path, t.format, t.duration, "
            "t.artist, t.album_artist, t.album, t.title, t.status, t.bitrate, "
            "t.sample_rate, t.bit_depth, t.channels "
            "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position, pt.id",
            (playlist_id,),
        )
        return {**dict(row), "tracks": [dict(track) for track in tracks]}

    def append_playlist_tracks(
        self, playlist_id: int, track_ids: Sequence[int], owner_id: Optional[int] = None,
    ) -> bool:
        with self._lock, self._conn:
            owner_filter = "" if owner_id is None else " AND owner_id=?"
            params = (playlist_id,) if owner_id is None else (playlist_id, owner_id)
            playlist = self._conn.execute(
                f"SELECT id FROM playlists WHERE id=?{owner_filter}", params
            ).fetchone()
            if playlist is None:
                return False
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next_position "
                "FROM playlist_tracks WHERE playlist_id=?",
                (playlist_id,),
            ).fetchone()
            start = row["next_position"]
            now = utcnow()
            self._conn.executemany(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) "
                "VALUES (?,?,?,?)",
                [
                    (playlist_id, track_id, start + index, now)
                    for index, track_id in enumerate(track_ids)
                ],
            )
            self._conn.execute(
                "UPDATE playlists SET updated_at=? WHERE id=?", (now, playlist_id)
            )
            return True

    def remove_playlist_track(
        self, playlist_id: int, entry_id: int, owner_id: Optional[int] = None,
    ) -> bool:
        with self._lock, self._conn:
            owner_filter = "" if owner_id is None else " AND owner_id=?"
            owner_params = (playlist_id,) if owner_id is None else (playlist_id, owner_id)
            cursor = self._conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id=? AND id=? "
                f"AND playlist_id IN (SELECT id FROM playlists WHERE id=?{owner_filter})",
                (playlist_id, entry_id, *owner_params),
            )
            if not cursor.rowcount:
                return False
            rows = self._conn.execute(
                "SELECT id FROM playlist_tracks WHERE playlist_id=? ORDER BY position, id",
                (playlist_id,),
            ).fetchall()
            self._conn.executemany(
                "UPDATE playlist_tracks SET position=? WHERE id=?",
                [(position, row["id"]) for position, row in enumerate(rows)],
            )
            self._conn.execute(
                "UPDATE playlists SET updated_at=? WHERE id=?", (utcnow(), playlist_id)
            )
            return True

    def reorder_playlist_tracks(
        self, playlist_id: int, entry_ids: Sequence[int],
        owner_id: Optional[int] = None,
    ) -> bool:
        with self._lock, self._conn:
            owner_filter = "" if owner_id is None else " AND owner_id=?"
            params = (playlist_id,) if owner_id is None else (playlist_id, owner_id)
            playlist = self._conn.execute(
                f"SELECT id FROM playlists WHERE id=?{owner_filter}", params
            ).fetchone()
            if playlist is None:
                return False
            current = [
                row["id"] for row in self._conn.execute(
                    "SELECT id FROM playlist_tracks WHERE playlist_id=? ORDER BY position, id",
                    (playlist_id,),
                )
            ]
            if len(current) != len(entry_ids) or set(current) != set(entry_ids):
                raise ValueError("Playlist order must include each current item exactly once.")
            self._conn.executemany(
                "UPDATE playlist_tracks SET position=? WHERE playlist_id=? AND id=?",
                [
                    (position, playlist_id, entry_id)
                    for position, entry_id in enumerate(entry_ids)
                ],
            )
            self._conn.execute(
                "UPDATE playlists SET updated_at=? WHERE id=?", (utcnow(), playlist_id)
            )
            return True

    def find_track_by_path(self, path: str) -> Optional[Track]:
        row = self.query_one("SELECT * FROM tracks WHERE path=?", (path,))
        return self._row_to_track(row) if row else None

    def album_groups_page(
        self, query: str = "", status: str = "", genre: str = "", format: str = "",
        offset: int = 0, limit: int = 48, initial: str = "",
        listening: bool = False, sort: str = "artist", album_artist: str = "",
    ) -> Dict[str, Any]:
        artist_expr = (
            "COALESCE(NULLIF(TRIM(album_artist), ''), NULLIF(TRIM(artist), ''), 'Unknown Artist')"
        )
        album_expr = "COALESCE(NULLIF(TRIM(album), ''), 'Unknown Album')"
        where = "status IN ('ok', 'suspect')" if listening else "status IN ('ok', 'suspect', 'bad')"
        params: List[Any] = []
        if album_artist:
            where += f" AND {artist_expr}=?"
            params.append(album_artist)
        if status in ("ok", "suspect", "bad"):
            where += " AND status=?"
            params.append(status)
        for column, value in (("genre", genre), ("format", format)):
            if value:
                where += f" AND {column}=?"
                params.append(value)
        if initial == "#":
            where += f" AND UPPER(SUBSTR({artist_expr}, 1, 1)) NOT GLOB '[A-Z]'"
        elif initial:
            where += f" AND UPPER(SUBSTR({artist_expr}, 1, 1)) = ?"
            params.append(initial)
        if listening:
            clause, search_params = music_search_clause(query)
            where += clause
            params.extend(search_params)
        elif query:
            pattern = f"%{query}%"
            where += (
                " AND (path LIKE ? COLLATE NOCASE OR artist LIKE ? COLLATE NOCASE "
                "OR album_artist LIKE ? COLLATE NOCASE OR album LIKE ? COLLATE NOCASE "
                "OR title LIKE ? COLLATE NOCASE)"
            )
            params.extend([pattern] * 5)
        grouped = (
            f"SELECT {artist_expr} AS album_artist, {album_expr} AS album, "
            "MAX(year) AS year, MAX(genre) AS genre, COUNT(*) AS track_count, MAX(id) AS latest_id, "
            "SUM(CASE WHEN status='bad' THEN 1 ELSE 0 END) AS bad_count, "
            "SUM(CASE WHEN has_embedded_art=1 THEN 1 ELSE 0 END) AS art_count, "
            "MAX(mbid_release) AS mbid_release, "
            "MIN(CASE WHEN has_embedded_art=1 THEN path END) AS cover_path "
            f"FROM tracks WHERE {where} GROUP BY 1, 2"
        )
        total = self.query_one(f"SELECT COUNT(*) AS n FROM ({grouped})", params)["n"]
        ordering = {
            "artist": "g.album_artist COLLATE NOCASE, g.album COLLATE NOCASE",
            "title": "g.album COLLATE NOCASE, g.album_artist COLLATE NOCASE",
            "recent": "g.latest_id DESC",
        }[sort]
        rows = self.query(
            "SELECT g.*, cover.id AS cover_track_id, cover.file_hash AS cover_hash FROM (" + grouped + ") AS g "
            "LEFT JOIN tracks AS cover ON cover.path=g.cover_path "
            f"ORDER BY {ordering}, g.album_artist, g.album "
            "LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return {"items": [dict(row) for row in rows], "total": total, "offset": offset}

    def listening_artists_page(
        self, query: str = "", offset: int = 0, limit: int = 25, sort: str = "artist",
    ) -> Dict[str, Any]:
        artist = "COALESCE(NULLIF(TRIM(album_artist), ''), NULLIF(TRIM(artist), ''), 'Unknown Artist')"
        album = "COALESCE(NULLIF(TRIM(album), ''), 'Unknown Album')"
        params: List[Any] = []
        where = "status IN ('ok', 'suspect')"
        for word in query.split():
            pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where += f" AND {artist} LIKE ? ESCAPE '\\' COLLATE NOCASE"
            params.append(pattern)
        grouped = (
            f"SELECT {artist} AS name, COUNT(*) AS track_count, "
            f"COUNT(DISTINCT {album}) AS album_count, MAX(id) AS latest_id "
            f"FROM tracks WHERE {where} GROUP BY 1"
        )
        total = self.query_one(f"SELECT COUNT(*) AS n FROM ({grouped})", params)["n"]
        order = "latest_id DESC, name COLLATE NOCASE, name" if sort == "recent" else "name COLLATE NOCASE, name"
        rows = self.query(
            f"SELECT name, track_count, album_count FROM ({grouped}) "
            f"ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset],
        )
        return {"items": [dict(row) for row in rows], "total": total, "offset": offset}

    def album_tracks(
        self, album_artist: str, album: str, status: str = "",
        listening: bool = False, favorite_user_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        artist_expr = (
            "COALESCE(NULLIF(TRIM(album_artist), ''), NULLIF(TRIM(artist), ''), 'Unknown Artist')"
        )
        album_expr = "COALESCE(NULLIF(TRIM(album), ''), 'Unknown Album')"
        where = (
            f"status IN ('ok', 'suspect', 'bad') AND {artist_expr}=? AND {album_expr}=?"
        )
        params: List[Any] = [album_artist, album]
        if listening:
            where += " AND status IN ('ok', 'suspect')"
        if status in ("ok", "suspect", "bad"):
            where += " AND status=?"
            params.append(status)
        favorite = (
            "EXISTS (SELECT 1 FROM favorites f WHERE f.user_id=? AND f.track_id=tracks.id)"
            if favorite_user_id is not None else "0"
        )
        query_params = ([favorite_user_id] if favorite_user_id is not None else []) + params
        rows = self.query(
            "SELECT id, path, artist, album_artist, album, title, genre, format, "
            f"bitrate, duration, year, track_no, disc_no, status, mbid_release, has_embedded_art, file_hash, "
            f"{favorite} AS is_favorite "
            f"FROM tracks WHERE {where} ORDER BY disc_no, track_no, path",
            query_params,
        )
        return [
            {**dict(row), "is_favorite": bool(row["is_favorite"])} for row in rows
        ]

    def listening_tracks_page(
        self, query: str = "", offset: int = 0, limit: int = 25, sort: str = "artist",
        favorite_user_id: Optional[int] = None, favorites_only: bool = False,
    ) -> Dict[str, Any]:
        clause, params = music_search_clause(query)
        where = "status IN ('ok', 'suspect')" + clause
        if favorites_only:
            if favorite_user_id is None:
                return {"items": [], "total": 0, "offset": offset}
            where += " AND EXISTS (SELECT 1 FROM favorites f WHERE f.user_id=? AND f.track_id=tracks.id)"
            params.append(favorite_user_id)
        ordering = {
            "artist": "artist COLLATE NOCASE, album COLLATE NOCASE, disc_no, track_no",
            "title": "title COLLATE NOCASE, artist COLLATE NOCASE",
            "recent": "id DESC",
        }[sort]
        ordering_params: List[Any] = []
        if favorites_only and sort == "recent":
            ordering = (
                "(SELECT created_at FROM favorites f "
                "WHERE f.user_id=? AND f.track_id=tracks.id) DESC, id DESC"
            )
            ordering_params.append(favorite_user_id)
        total = self.query_one(f"SELECT COUNT(*) AS n FROM tracks WHERE {where}", params)["n"]
        favorite = (
            "EXISTS (SELECT 1 FROM favorites f WHERE f.user_id=? AND f.track_id=tracks.id)"
            if favorite_user_id is not None else "0"
        )
        row_params = ([favorite_user_id] if favorite_user_id is not None else []) + params
        rows = self.query(
            "SELECT id, title, artist, album_artist, album, duration, genre, format, disc_no, track_no "
            f", {favorite} AS is_favorite "
            f"FROM tracks WHERE {where} ORDER BY {ordering}, id LIMIT ? OFFSET ?",
            row_params + ordering_params + [limit, offset],
        )
        items = [dict(row) for row in rows]
        for item in items:
            item["is_favorite"] = bool(item["is_favorite"])
        return {"items": items, "total": total, "offset": offset}

    def add_favorite(self, user_id: int, track_id: int) -> bool:
        cursor = self.execute(
            "INSERT OR IGNORE INTO favorites (user_id, track_id, created_at) VALUES (?,?,?)",
            (user_id, track_id, utcnow()),
        )
        return cursor.rowcount > 0

    def remove_favorite(self, user_id: int, track_id: int) -> bool:
        cursor = self.execute(
            "DELETE FROM favorites WHERE user_id=? AND track_id=?", (user_id, track_id),
        )
        return cursor.rowcount > 0

    def is_favorite(self, user_id: int, track_id: int) -> bool:
        return self.query_one(
            "SELECT 1 FROM favorites WHERE user_id=? AND track_id=?", (user_id, track_id),
        ) is not None

    def all_tracks(self, status: Optional[str] = None) -> List[Track]:
        if status:
            rows = self.query("SELECT * FROM tracks WHERE status=? ORDER BY path", (status,))
        else:
            rows = self.query("SELECT * FROM tracks ORDER BY path")
        return [self._row_to_track(r) for r in rows]

    def tracks_missing_fingerprint(self) -> List[Track]:
        rows = self.query(
            "SELECT * FROM tracks WHERE fingerprint IS NULL AND status IN ('ok','suspect') ORDER BY path"
        )
        return [self._row_to_track(r) for r in rows]

    def _row_to_track(self, row: sqlite3.Row) -> Track:
        track = Track(
            id=row["id"], path=row["path"], mtime=row["mtime"], size=row["size"],
            format=row["format"], bitrate=row["bitrate"], sample_rate=row["sample_rate"],
            bit_depth=row["bit_depth"], channels=row["channels"], duration=row["duration"],
            artist=row["artist"], album_artist=row["album_artist"], album=row["album"],
            title=row["title"], track_no=row["track_no"], track_total=row["track_total"],
            disc_no=row["disc_no"], disc_total=row["disc_total"], year=row["year"],
            genre=row["genre"], mbid_track=row["mbid_track"],
            mbid_release=row["mbid_release"], has_embedded_art=bool(row["has_embedded_art"]),
            file_hash=row["file_hash"], fingerprint=row["fingerprint"],
            quality_score=row["quality_score"], status=row["status"],
        )
        issue_rows = self.query(
            "SELECT kind, detail, severity FROM issues WHERE track_id=?", (track.id,)
        )
        track.issues = [Issue(r["kind"], r["detail"], r["severity"]) for r in issue_rows]
        return track

    def issues_all(self) -> List[sqlite3.Row]:
        return self.query(
            "SELECT i.*, t.path FROM issues i JOIN tracks t ON t.id = i.track_id "
            "WHERE t.status != 'deleted' ORDER BY i.severity DESC, t.path"
        )

    def upsert_album(self, album: Album) -> Album:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO albums (album_artist, album, year, mbid_release,
                    mbid_verified, art_status, expected_tracks, have_tracks,
                    missing_tracks)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(album_artist, album) DO UPDATE SET
                    year=excluded.year, mbid_release=excluded.mbid_release,
                    mbid_verified=excluded.mbid_verified,
                    art_status=excluded.art_status,
                    expected_tracks=excluded.expected_tracks,
                    have_tracks=excluded.have_tracks,
                    missing_tracks=excluded.missing_tracks
                """,
                (
                    album.album_artist, album.album, album.year, album.mbid_release,
                    int(album.mbid_verified), album.art_status, album.expected_tracks,
                    album.have_tracks, album.missing_tracks,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM albums WHERE album_artist=? AND album=?",
                (album.album_artist, album.album),
            ).fetchone()
            album.id = row["id"]
        return album

    def albums_all(self, where: str = "1=1", params: Sequence = ()) -> List[Album]:
        rows = self.query(
            f"SELECT * FROM albums WHERE {where} ORDER BY album_artist, album", params
        )
        return [self._row_to_album(r) for r in rows]

    def reconcile_albums(self, folder_images: Set[str]) -> None:
        """Replace derived summaries atomically, retaining only valid catalog caches."""
        with self._lock, self._conn:
            groups: Dict[tuple, list] = {}
            for row in self._conn.execute(
                "SELECT id, path, album_artist, album, year, mbid_release, has_embedded_art, "
                "title, disc_no, track_no, "
                "LOWER(COALESCE(NULLIF(album_artist,''),'Unknown Artist')) AS artist_key, "
                "LOWER(album) AS album_key FROM tracks "
                "WHERE status IN ('ok','suspect') AND album IS NOT NULL AND album != '' ORDER BY id"
            ):
                groups.setdefault((row["artist_key"], row["album_key"]), []).append(row)
            existing: Dict[tuple, list] = {}
            for row in self._conn.execute(
                "SELECT *, LOWER(album_artist) AS artist_key, LOWER(album) AS album_key "
                "FROM albums ORDER BY mbid_verified DESC, id"
            ):
                existing.setdefault((row["artist_key"], row["album_key"]), []).append(row)
            retained = set()
            for key, tracks in groups.items():
                previous = existing.get(key, [])
                old = previous[0] if previous else None
                signature = hashlib.sha256(json.dumps([
                    [t["id"], t["title"], t["disc_no"], t["track_no"]]
                    for t in tracks
                ]).encode()).hexdigest()
                releases = sorted({t["mbid_release"] for t in tracks if t["mbid_release"]})
                release_signature = json.dumps(releases)
                release_changed = old is not None and old["release_signature"] is not None and (
                    old["release_signature"] != release_signature
                )
                unchanged = old is not None and len(previous) == 1 and (
                    old["track_signature"] == signature and not release_changed
                )
                tagged_release = releases[0] if len(releases) == 1 else None
                mbid = old["mbid_release"] if old and not release_changed else tagged_release
                verified = old["mbid_verified"] if old and not release_changed else 0
                years = [t["year"] for t in tracks if t["year"] is not None]
                year = min(years) if years else (old["year"] if old else None)
                has_art = any(
                    t["has_embedded_art"] or str(Path(t["path"]).parent) in folder_images
                    for t in tracks
                )
                art_status = "ok" if has_art else "missing"
                if old and (
                    (has_art and old["art_status"] == "downloaded")
                    or (not has_art and unchanged and old["art_status"] in ("none", "failed", "small"))
                ):
                    art_status = old["art_status"]
                values = (
                    old["album_artist"] if old else tracks[0]["album_artist"] or "Unknown Artist",
                    old["album"] if old else tracks[0]["album"],
                    year, mbid, verified, art_status,
                    old["expected_tracks"] if old and not release_changed else None,
                    len(tracks), old["missing_tracks"] if unchanged else None,
                    signature, release_signature,
                )
                if old:
                    self._conn.execute(
                        "UPDATE albums SET album_artist=?, album=?, year=?, mbid_release=?, "
                        "mbid_verified=?, art_status=?, expected_tracks=?, have_tracks=?, "
                        "missing_tracks=?, track_signature=?, release_signature=? WHERE id=?",
                        values + (old["id"],),
                    )
                    retained.add(old["id"])
                else:
                    cursor = self._conn.execute(
                        "INSERT INTO albums (album_artist,album,year,mbid_release,mbid_verified,"
                        "art_status,expected_tracks,have_tracks,missing_tracks,"
                        "track_signature,release_signature) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                    retained.add(cursor.lastrowid)
            self._conn.executemany(
                "DELETE FROM albums WHERE id=?",
                [(row["id"],) for rows in existing.values() for row in rows if row["id"] not in retained],
            )
            self.albums_dirty = False

    def _row_to_album(self, row: sqlite3.Row) -> Album:
        return Album(
            id=row["id"], album_artist=row["album_artist"], album=row["album"],
            year=row["year"], mbid_release=row["mbid_release"],
            mbid_verified=bool(row["mbid_verified"]), art_status=row["art_status"],
            expected_tracks=row["expected_tracks"], have_tracks=row["have_tracks"],
            missing_tracks=row["missing_tracks"],
        )

    def record_deletion(
        self, path: str, size: int, file_hash: Optional[str], reason: str,
        moved_to: Optional[str],
    ) -> int:
        cursor = self.execute(
            "INSERT INTO deletions (path, size, file_hash, reason, moved_to, deleted_at) "
            "VALUES (?,?,?,?,?,?)",
            (path, size, file_hash, reason, moved_to, utcnow()),
        )
        if cursor.lastrowid is None:
            raise sqlite3.DatabaseError("Recovery history did not return a deletion ID.")
        return cursor.lastrowid

    def deletions_page(
        self, offset: int = 0, limit: int = 50, include_restored: bool = False
    ) -> tuple:
        where = "1=1" if include_restored else "restored_at IS NULL AND moved_to IS NOT NULL"
        total = self.query_one(f"SELECT COUNT(*) AS n FROM deletions WHERE {where}")["n"]
        rows = self.query(
            f"SELECT * FROM deletions WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(row) for row in rows], total

    def mark_restored(self, deletion_id: int) -> None:
        self.execute(
            "UPDATE deletions SET restored_at=? WHERE id=? AND restored_at IS NULL",
            (utcnow(), deletion_id),
        )

    def record_download(
        self, nzo_id: str, title: str, url: str, indexer: str, status: str = "queued"
    ) -> None:
        self.execute(
            "INSERT INTO downloads (nzo_id, title, url, indexer, status, requested_at) "
            "VALUES (?,?,?,?,?,?)",
            (nzo_id, title, url, indexer, status, utcnow()),
        )

    def update_download_status(self, nzo_id: str, status: str) -> None:
        completed = utcnow() if status in ("completed", "failed") else None
        self.execute(
            "UPDATE downloads SET status=?, completed_at=COALESCE(?, completed_at) "
            "WHERE nzo_id=?",
            (status, completed, nzo_id),
        )

    def stats(self) -> Dict[str, Any]:
        row = self.query_one(
            """
            SELECT COUNT(*) AS tracks,
                   COALESCE(SUM(size), 0) AS bytes,
                   (SELECT COUNT(*) FROM albums) AS albums,
                   COALESCE(SUM(duration), 0) AS seconds,
                   COALESCE(SUM(CASE WHEN has_embedded_art THEN 1 ELSE 0 END), 0) AS with_art
            FROM tracks WHERE status IN ('ok', 'suspect')
            """
        )
        issues = self.query_one("SELECT COUNT(*) AS n FROM issues")["n"]
        dupes = self.query_one(
            "SELECT COUNT(*) AS n FROM dupe_groups g WHERE resolved=0 AND "
            "(SELECT COUNT(*) FROM dupe_members m JOIN tracks t ON t.id=m.track_id "
            "WHERE m.group_id=g.id AND t.status IN ('ok', 'suspect')) >= 2"
        )["n"]
        albums_missing_art = self.query_one(
            "SELECT COUNT(*) AS n FROM albums WHERE art_status IN ('missing', 'none')"
        )["n"]
        albums_missing_tracks = self.query_one(
            "SELECT COUNT(*) AS n FROM albums WHERE missing_tracks IS NOT NULL "
            "AND missing_tracks != '[]'"
        )["n"]
        return {
            "tracks": row["tracks"],
            "bytes": row["bytes"],
            "albums": row["albums"],
            "duration_seconds": row["seconds"],
            "with_art": row["with_art"],
            "issues": issues,
            "dupe_groups": dupes,
            "albums_missing_art": albums_missing_art,
            "albums_missing_tracks": albums_missing_tracks,
        }
