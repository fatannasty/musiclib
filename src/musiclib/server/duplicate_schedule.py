from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from fastapi import HTTPException

from ..config import Config
from ..db import Database
from .jobs import JobRegistry

logger = logging.getLogger(__name__)


class DuplicateSchedule:
    """Opt-in NAS-local discovery; never applies a cleanup."""

    def __init__(self, config: Config, db: Database, jobs: JobRegistry, discover: Callable):
        self.config, self.db, self.jobs, self.discover = config, db, jobs, discover
        self.stop = threading.Event()
        self.thread = None
        db.execute(
            "CREATE TABLE IF NOT EXISTS duplicate_schedule "
            "(id INTEGER PRIMARY KEY CHECK(id=1), hours INTEGER NOT NULL, next_run REAL NOT NULL)"
        )

    def status(self, now: float = None) -> dict:
        now = time.time() if now is None else now
        hours = int(self.config.duplicates.get("schedule_hours", 0))
        if hours not in (0, 24, 168):
            raise ValueError("Duplicate schedule_hours must be 0 (off), 24 (daily), or 168 (weekly).")
        row = self.db.query_one("SELECT * FROM duplicate_schedule WHERE id=1")
        if row is None or row["hours"] != hours:
            self.db.execute(
                "INSERT INTO duplicate_schedule VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET hours=excluded.hours,next_run=excluded.next_run",
                (hours, now + hours * 3600),
            )
            row = self.db.query_one("SELECT * FROM duplicate_schedule WHERE id=1")
        return {"hours": hours, "next_run": row["next_run"] if hours else None}

    def tick(self, now: float = None) -> None:
        now = time.time() if now is None else now
        status = self.status(now)
        if not status["hours"] or status["next_run"] > now:
            return
        try:
            self.jobs.start("duplicates", self.discover, origin="schedule")
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return
        self.db.execute(
            "UPDATE duplicate_schedule SET next_run=? WHERE id=1",
            (now + status["hours"] * 3600,),
        )

    def start(self) -> None:
        self.status()

        def watch():
            while not self.stop.wait(30):
                try:
                    self.tick()
                except Exception:
                    logger.exception("Scheduled duplicate discovery could not start")

        self.thread = threading.Thread(target=watch, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join()
