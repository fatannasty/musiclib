from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException

from ..config import Config
from ..db import Database
from .jobs import JobRegistry

logger = logging.getLogger(__name__)


class ArtworkSchedule:
    """Opt-in, read-only cover discovery that advances through missing-art albums."""

    BATCH_SIZE = 25

    def __init__(
        self, config: Config, db: Database, jobs: JobRegistry,
        discover: Callable[[Dict[str, Any], list], Dict[str, Any]],
    ):
        self.config, self.db, self.jobs, self.discover = config, db, jobs, discover
        self.stop = threading.Event()
        self.thread = None
        db.execute(
            "CREATE TABLE IF NOT EXISTS artwork_schedule "
            "(id INTEGER PRIMARY KEY CHECK(id=1), hours INTEGER NOT NULL, "
            "next_run REAL NOT NULL, cursor_id INTEGER NOT NULL DEFAULT 0)"
        )

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        hours = int(self.config.artwork.get("schedule_hours", 0))
        if hours not in (0, 24, 168):
            raise ValueError("Artwork schedule_hours must be 0 (off), 24 (daily), or 168 (weekly).")
        row = self.db.query_one("SELECT * FROM artwork_schedule WHERE id=1")
        if row is None:
            self.db.execute(
                "INSERT INTO artwork_schedule VALUES(1,?,?,0)",
                (hours, now + hours * 3600 if hours else 0),
            )
            row = self.db.query_one("SELECT * FROM artwork_schedule WHERE id=1")
        elif row["hours"] != hours:
            self.db.execute(
                "UPDATE artwork_schedule SET hours=?,next_run=? WHERE id=1",
                (hours, now + hours * 3600 if hours else 0),
            )
            row = self.db.query_one("SELECT * FROM artwork_schedule WHERE id=1")
        return {
            "hours": hours,
            "next_run": row["next_run"] if hours else None,
            "cursor_id": row["cursor_id"],
        }

    def _batch(self, cursor_id: int) -> tuple[list, bool]:
        rows = self.db.query(
            "SELECT id,album_artist,album FROM albums "
            "WHERE art_status IN ('missing','none','unknown','failed') AND id>? "
            "ORDER BY id LIMIT ?",
            (cursor_id, self.BATCH_SIZE + 1),
        )
        if not rows and cursor_id:
            rows = self.db.query(
                "SELECT id,album_artist,album FROM albums "
                "WHERE art_status IN ('missing','none','unknown','failed') "
                "ORDER BY id LIMIT ?",
                (self.BATCH_SIZE + 1,),
            )
        return rows[:self.BATCH_SIZE], len(rows) > self.BATCH_SIZE

    def start_next(self, origin: str = "manual", now: Optional[float] = None) -> Optional[str]:
        now = time.time() if now is None else now
        status = self.status(now)
        rows, has_more = self._batch(status["cursor_id"])
        if not rows:
            if origin == "schedule" and status["hours"]:
                self.db.execute(
                    "UPDATE artwork_schedule SET next_run=? WHERE id=1",
                    (now + status["hours"] * 3600,),
                )
            return None

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            result = self.discover(job, rows)
            cursor_id = rows[-1]["id"] if has_more else 0
            self.db.execute("UPDATE artwork_schedule SET cursor_id=? WHERE id=1", (cursor_id,))
            return result

        job_id = self.jobs.start(
            "artwork-discovery", run, read_only=True, origin=origin, lane="artwork",
        )
        if origin == "schedule" and status["hours"]:
            self.db.execute(
                "UPDATE artwork_schedule SET next_run=? WHERE id=1",
                (now + status["hours"] * 3600,),
            )
        return job_id

    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        status = self.status(now)
        if not status["hours"] or status["next_run"] > now:
            return
        try:
            self.start_next("schedule", now)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise

    def start(self) -> None:
        self.status()

        def watch() -> None:
            while not self.stop.wait(30):
                try:
                    self.tick()
                except Exception:
                    logger.exception("Scheduled artwork discovery could not start")

        self.thread = threading.Thread(target=watch, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join()
