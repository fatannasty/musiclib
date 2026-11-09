from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import uuid
from functools import wraps
from typing import Any, Dict, Optional, get_type_hints

from fastapi import HTTPException

from ..db import Database

logger = logging.getLogger(__name__)


class JobRegistry:
    """Persist activity and allow only one library operation at a time."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lease = threading.Lock()
        self._local = threading.local()
        self._threads: list = []
        db.execute(
            "CREATE TABLE IF NOT EXISTS jobs "
            "(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, result TEXT)"
        )
        for job in self.list():
            if job["status"] in ("queued", "running"):
                job.update(
                    status="error", finished=time.time(),
                    error="Application restarted before this job finished. Review results before retrying.",
                )
                self._save(job)

    def _save(self, job: Dict[str, Any], result: Any = None) -> None:
        self.db.execute(
            "INSERT INTO jobs (id, snapshot, result) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot, "
            "result=COALESCE(excluded.result, jobs.result)",
            (job["id"], json.dumps(job), json.dumps(result) if result is not None else None),
        )

    def operation(self, fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            if getattr(self._local, "owned", False):
                return fn(*args, **kwargs)
            if not self._lease.acquire(blocking=False):
                raise HTTPException(409, "A library operation is already running. See Activity.")
            self._local.owned = True
            try:
                return fn(*args, **kwargs)
            finally:
                self._local.owned = False
                self._lease.release()
        hints = get_type_hints(fn)
        signature = inspect.signature(fn)
        guarded.__signature__ = signature.replace(
            parameters=[
                param.replace(annotation=hints.get(name, param.annotation))
                for name, param in signature.parameters.items()
            ],
            return_annotation=hints.get("return", signature.return_annotation),
        )
        return guarded

    def start(self, kind: str, fn) -> str:
        if not self._lease.acquire(blocking=False):
            raise HTTPException(409, "A library operation is already running. See Activity.")
        job = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued",
            "started": time.time(), "finished": None, "phase": "Preparing",
            "completed": 0, "total": None, "current": "", "error": None,
            "summary": None,
        }
        try:
            self._save(job)
        except Exception:
            self._lease.release()
            raise

        def runner() -> None:
            self._local.owned = True
            self._local.job = job
            try:
                job["status"] = "running"
                self._save(job)
                result = fn(job)
                job.update(
                    status="done", finished=time.time(), phase="Complete",
                    summary={
                        key: len(value) if isinstance(value, list) else value
                        for key, value in result.items()
                    },
                )
                self._save(job, result)
            except Exception as exc:
                logger.exception("Musiclib %s job %s failed", kind, job["id"])
                job.update(status="error", finished=time.time(), error=str(exc))
                self._save(job)
            finally:
                self._local.owned = False
                self._local.job = None
                self._lease.release()

        thread = threading.Thread(target=runner, daemon=True)
        self._threads.append(thread)
        thread.start()
        return job["id"]

    def progress(
        self, job: Dict[str, Any], phase: str, completed: int = 0,
        total: Optional[int] = None, current: str = "",
    ) -> None:
        job.update(phase=phase, completed=completed, total=total, current=current)
        self._save(job)

    def update(
        self, phase: str, completed: int = 0, total: Optional[int] = None, current: str = "",
    ) -> None:
        job = getattr(self._local, "job", None)
        if job is not None:
            self.progress(job, phase, completed, total, current)

    def list(self) -> list:
        return [
            json.loads(row["snapshot"])
            for row in self.db.query("SELECT snapshot FROM jobs ORDER BY rowid DESC")
        ]

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT snapshot FROM jobs WHERE id=?", (job_id,))
        return json.loads(row["snapshot"]) if row else None

    def result(self, job_id: str) -> Dict[str, Any]:
        row = self.db.query_one("SELECT result FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(404, "job not found")
        if row["result"] is None:
            raise HTTPException(409, "Job results are not available. See Activity for status.")
        return json.loads(row["result"])

    def close(self) -> None:
        for thread in self._threads:
            thread.join()
