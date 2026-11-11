from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import uuid
from concurrent.futures import CancelledError
from functools import wraps
from typing import Any, Callable, Dict, Optional, get_type_hints

from fastapi import HTTPException

from ..db import Database

logger = logging.getLogger(__name__)


class JobRegistry:
    """Persist activity, with separate serialized lanes for searches and writes."""

    def __init__(self, db: Database, after_write: Optional[Callable[[], None]] = None) -> None:
        self.db = db
        self.after_write = after_write
        self._lease = threading.Lock()
        self._lookup_lease = threading.Lock()
        self._local = threading.local()
        self._threads: list = []
        self._state_lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._cancellations: Dict[str, threading.Event] = {}
        db.execute(
            "CREATE TABLE IF NOT EXISTS jobs "
            "(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, result TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS job_items ("
            "job_id TEXT NOT NULL, item_key TEXT NOT NULL, status TEXT NOT NULL, "
            "detail TEXT NOT NULL, PRIMARY KEY(job_id,item_key))"
        )
        for job in self.list():
            if job["status"] in ("queued", "running", "stopping"):
                self._finish_items(job, "stopped", "Application restarted before this item finished. Check the file before retrying.")
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
        return self._guard(fn, read_only=False)

    def lookup_operation(self, fn):
        return self._guard(fn, read_only=True)

    def _guard(self, fn, read_only: bool):
        lease = self._lookup_lease if read_only else self._lease
        ownership = "lookup_owned" if read_only else "owned"

        @wraps(fn)
        def guarded(*args, **kwargs):
            if getattr(self._local, ownership, False):
                return fn(*args, **kwargs)
            if not lease.acquire(blocking=False):
                raise self._busy(read_only)
            setattr(self._local, ownership, True)
            try:
                try:
                    return fn(*args, **kwargs)
                finally:
                    if not read_only and self.after_write:
                        self.after_write()
            finally:
                setattr(self._local, ownership, False)
                lease.release()
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

    @staticmethod
    def _busy(read_only: bool) -> HTTPException:
        message = (
            "A catalog search is already running. Wait for it to finish; bulk progress is in Activity."
            if read_only else "A library operation is already running. See Activity."
        )
        return HTTPException(409, message)

    def start(self, kind: str, fn, *, read_only: bool = False, origin: str = "manual") -> str:
        lease = self._lookup_lease if read_only else self._lease
        ownership = "lookup_owned" if read_only else "owned"
        if not lease.acquire(blocking=False):
            raise self._busy(read_only)
        job = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued",
            "started": time.time(), "finished": None, "phase": "Preparing",
            "completed": 0, "total": None, "current": "", "error": None,
            "summary": None, "read_only": read_only,
            "cancel_requested": False,
            "origin": origin,
        }
        try:
            with self._state_lock:
                self._active[job["id"]] = job
                self._cancellations[job["id"]] = threading.Event()
                self._save(job)
        except Exception:
            self._active.pop(job["id"], None)
            self._cancellations.pop(job["id"], None)
            lease.release()
            raise

        def runner() -> None:
            setattr(self._local, ownership, True)
            self._local.job = job
            try:
                with self._state_lock:
                    self.check_cancelled()
                    job["status"] = "running"
                    self._save(job)
                try:
                    result = fn(job)
                finally:
                    if not read_only and self.after_write:
                        self.after_write()
                with self._state_lock:
                    self._finish_items(job, "stopped", "No completion was recorded for this item. Inspect the current file before retrying.")
                    job.update(
                        status="done", finished=time.time(), phase="Complete",
                        summary={
                            key: len(value) if isinstance(value, list) else value
                            for key, value in result.items()
                        },
                    )
                    self._save(job, result)
            except CancelledError:
                with self._state_lock:
                    self._finish_items(job, "stopped", "Not completed before Stop. Check the current file before retrying.")
                    job.update(
                        status="cancelled", finished=time.time(), phase="Stopped",
                        error=None,
                        summary={"processed": job["completed"], "total": job["total"]},
                    )
                    self._save(job)
            except Exception as exc:
                logger.exception("Musiclib %s job %s failed", kind, job["id"])
                with self._state_lock:
                    self._finish_items(job, "stopped", "Job failed before this item completed. Check the current file before retrying.")
                    job.update(status="error", finished=time.time(), error=str(exc))
                    self._save(job)
            finally:
                setattr(self._local, ownership, False)
                self._local.job = None
                with self._state_lock:
                    self._active.pop(job["id"], None)
                    self._cancellations.pop(job["id"], None)
                lease.release()

        thread = threading.Thread(target=runner, daemon=True)
        try:
            thread.start()
        except Exception as exc:
            job.update(status="error", finished=time.time(), error=str(exc))
            try:
                self._save(job)
            finally:
                self._active.pop(job["id"], None)
                self._cancellations.pop(job["id"], None)
                lease.release()
            raise
        self._threads.append(thread)
        return job["id"]

    @staticmethod
    def _item_key(item: Dict[str, Any]) -> str:
        for field in ("track_id", "deletion_id", "path"):
            if item.get(field) not in (None, ""):
                return f"{field}:{item[field]}"
        raise ValueError("A file outcome requires a track ID, recovery ID, or path.")

    def plan(self, items: list) -> None:
        job = getattr(self._local, "job", None)
        if job is None:
            return
        with self._state_lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO job_items(job_id,item_key,status,detail) VALUES (?,?,?,?)",
                [(job["id"], self._item_key(item), "pending", json.dumps(item)) for item in items],
            )
            self._item_counts(job)
            self._save(job)

    def outcome(self, item: Dict[str, Any]) -> None:
        job = getattr(self._local, "job", None)
        if job is None:
            return
        status = item["status"]
        if status not in ("pending", "completed", "failed", "skipped"):
            raise ValueError(f"Unknown file outcome: {status}")
        key = self._item_key(item)
        with self._state_lock:
            existing = self.db.query_one(
                "SELECT detail,status FROM job_items WHERE job_id=? AND item_key=?", (job["id"], key),
            )
            detail = {**(json.loads(existing["detail"]) if existing else {}), **item}
            self.db.execute(
                "INSERT INTO job_items(job_id,item_key,status,detail) VALUES (?,?,?,?) "
                "ON CONFLICT(job_id,item_key) DO UPDATE SET status=excluded.status,detail=excluded.detail",
                (job["id"], key, status, json.dumps(detail)),
            )
            counts = job.setdefault("outcomes", {})
            if existing:
                previous = existing["status"]
                counts[previous] -= 1
                if not counts[previous]:
                    del counts[previous]
            counts[status] = counts.get(status, 0) + 1
            self._save(job)

    def _item_counts(self, job: Dict[str, Any]) -> None:
        counts = {row["status"]: row["n"] for row in self.db.query(
            "SELECT status,COUNT(*) AS n FROM job_items WHERE job_id=? GROUP BY status",
            (job["id"],),
        )}
        if counts:
            job["outcomes"] = counts

    def _finish_items(self, job: Dict[str, Any], status: str, reason: str) -> None:
        for row in self.db.query(
            "SELECT item_key,detail FROM job_items WHERE job_id=? AND status='pending'", (job["id"],),
        ):
            item = {**json.loads(row["detail"]), "status": status, "reason": reason}
            self.db.execute(
                "UPDATE job_items SET status=?,detail=? WHERE job_id=? AND item_key=?",
                (status, json.dumps(item), job["id"], row["item_key"]),
            )
        self._item_counts(job)

    def items(
        self, job_id: str, status: str = "", offset: int = 0, limit: Optional[int] = None,
    ) -> list:
        params = [job_id, status] if status else [job_id]
        sql = "SELECT detail,status FROM job_items WHERE job_id=?"
        sql += (" AND status=?" if status else "") + " ORDER BY rowid"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        return [
            {**json.loads(row["detail"]), "status": row["status"]}
            for row in self.db.query(sql, params)
        ]

    def item_count(self, job_id: str, status: str = "") -> int:
        return self.db.query_one(
            "SELECT COUNT(*) AS n FROM job_items WHERE job_id=?" + (" AND status=?" if status else ""),
            (job_id, status) if status else (job_id,),
        )["n"]

    def progress(
        self, job: Dict[str, Any], phase: str, completed: int = 0,
        total: Optional[int] = None, current: str = "",
    ) -> None:
        with self._state_lock:
            job.update(phase=phase, completed=completed, total=total, current=current)
            self._save(job)
            self.check_cancelled()

    def check_cancelled(self) -> None:
        job = getattr(self._local, "job", None)
        if job is not None:
            event = self._cancellations.get(job["id"])
            if event is not None and event.is_set():
                raise CancelledError("Job stopped at a safe checkpoint.")

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._state_lock:
            job = self._active.get(job_id)
            if job is None:
                if self.get(job_id) is None:
                    raise HTTPException(404, "job not found")
                raise HTTPException(409, "This job has already finished.")
            if job["status"] not in ("queued", "running", "stopping"):
                raise HTTPException(409, "This job has already finished.")
            self._cancellations[job_id].set()
            job.update(status="stopping", cancel_requested=True)
            self._save(job)
            return dict(job)

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
