from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .db import Database
from .fsops import case_insensitive_exists, is_inside, move_no_replace
from .models import Track
from .scanner import file_hash


class TrashReview:
    def __init__(self, config: Config, db: Database):
        self.db = db
        self.library = str(Path(config.library).expanduser().resolve())
        self.trash_root = str(Path(config.trash_dir).expanduser().resolve())
        if is_inside(self.library, self.trash_root):
            raise ValueError("The trash folder must not be the library or contain the library.")
        db.execute(
            "CREATE TABLE IF NOT EXISTS trash_reviews "
            "(id TEXT PRIMARY KEY, library TEXT NOT NULL, trash_root TEXT NOT NULL, "
            "tracks TEXT NOT NULL, status TEXT NOT NULL)"
        )

    def _source(self, track_id: int, snapshot: Optional[Dict[str, Any]] = None) -> Track:
        track = self.db.get_track(track_id)
        if track is None or track.status == "deleted":
            raise ValueError("Track is no longer available.")
        path = Path(track.path)
        if not is_inside(track.path, self.library) or is_inside(track.path, self.trash_root):
            raise ValueError("File is outside the library or already inside trash.")
        if path.is_symlink():
            raise ValueError("Symbolic links cannot be trashed here. Select the original file.")
        if not path.is_file():
            raise ValueError("File is missing from disk.")
        if snapshot is not None and (
            track.path != snapshot["path"] or str(path.resolve()) != snapshot["source"]
            or track.file_hash != snapshot["catalog_hash"]
            or file_hash(track.path) != snapshot["file_hash"]
        ):
            raise ValueError("File moved or changed since preview. Create a fresh preview.")
        return track

    def _target_problem(self, source: str, target: str) -> str:
        path = Path(target)
        if not is_inside(target, self.trash_root) or str(path.resolve()) != target:
            return "Trash destination changed or is outside the configured trash folder."
        if os.path.lexists(target) or case_insensitive_exists(path.parent, path.name):
            return "Trash destination is occupied. No file will be overwritten."
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            return f"Trash parent is not a folder: {parent}"
        if not os.access(Path(source).parent, os.W_OK | os.X_OK):
            return "Cannot remove from the source folder. Check NAS permissions and PUID/PGID."
        if not os.access(parent, os.W_OK | os.X_OK):
            return "Cannot write to the trash folder. Check NAS permissions and PUID/PGID."
        return ""

    def preview(
        self, track_ids: List[int], progress: Optional[Callable[..., None]] = None,
        reason: str = "library:selected",
    ) -> Dict[str, Any]:
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise ValueError("Select between 1 and 200 distinct tracks.")
        review_id = uuid.uuid4().hex
        month = datetime.now().strftime("%Y-%m")
        snapshots, seen = [], set()
        for index, track_id in enumerate(track_ids):
            if progress:
                progress("Checking selected trash files", index, len(track_ids), str(track_id))
            snapshot: Dict[str, Any] = {"track_id": track_id, "status": "blocked", "reason": ""}
            track = self.db.get_track(track_id)
            if track is not None:
                snapshot.update(path=track.path, title=track.title)
            try:
                track = self._source(track_id)
                source = str(Path(track.path).resolve())
                if source in seen:
                    raise ValueError("This file is selected more than once.")
                seen.add(source)
                target = str(Path(self.trash_root, month, review_id, str(track_id), Path(source).name))
                snapshot.update(
                    source=source, target=target, file_hash=file_hash(source),
                    catalog_hash=track.file_hash, size=os.stat(source).st_size,
                    deletion_reason=reason,
                )
                reason = self._target_problem(source, target)
                if reason:
                    raise ValueError(reason)
                snapshot["status"] = "ready"
            except (OSError, ValueError) as exc:
                snapshot["reason"] = str(exc)
            snapshots.append(snapshot)
        self.db.execute(
            "INSERT INTO trash_reviews (id,library,trash_root,tracks,status) VALUES (?,?,?,?,?)",
            (review_id, self.library, self.trash_root, json.dumps(snapshots), "review"),
        )
        if progress:
            progress("Checking selected trash files", len(track_ids), len(track_ids), "")
        return {
            "review_id": review_id, "tracks": snapshots,
            "ready": sum(item["status"] == "ready" for item in snapshots),
            "blocked": sum(item["status"] == "blocked" for item in snapshots),
            "bytes": sum(item["size"] for item in snapshots if item["status"] == "ready"),
        }

    def status(self, review_id: str) -> str:
        row = self.db.query_one("SELECT status FROM trash_reviews WHERE id=?", (review_id,))
        if row is None:
            raise ValueError("Trash preview was not found.")
        return row["status"]

    def _move(self, snapshot: Dict[str, Any]) -> None:
        self._source(snapshot["track_id"], snapshot)
        reason = self._target_problem(snapshot["source"], snapshot["target"])
        if reason:
            raise ValueError(reason)
        deletion_id, moved = None, False

        def verify(target: str) -> None:
            nonlocal deletion_id
            if not is_inside(target, self.trash_root) or str(Path(target).resolve()) != target:
                raise ValueError("Trash destination changed. The source was not removed.")
            if file_hash(target) != snapshot["file_hash"]:
                raise ValueError("Trash copy differs from the preview. The source was not removed.")
            self._source(snapshot["track_id"], snapshot)
            # Persist recovery before removing the source, including for interrupted moves.
            deletion_id = self.db.record_deletion(
                snapshot["path"], snapshot["size"], snapshot["file_hash"],
                snapshot.get("deletion_reason", "library:selected"), target,
            )
            self._source(snapshot["track_id"], snapshot)

        try:
            move_no_replace(snapshot["source"], snapshot["target"], verify=verify)
            moved = True
        finally:
            if deletion_id is not None and not moved:
                self.db.execute("DELETE FROM deletions WHERE id=?", (deletion_id,))

    def apply(
        self, review_id: str, progress: Optional[Callable[..., None]] = None,
        outcome: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        row = self.db.query_one("SELECT * FROM trash_reviews WHERE id=?", (review_id,))
        if row is None or row["status"] != "review":
            raise ValueError("This trash preview is unavailable or already used. Create a fresh preview.")
        self.db.execute("UPDATE trash_reviews SET status='used' WHERE id=?", (review_id,))
        if (row["library"], row["trash_root"]) != (self.library, self.trash_root):
            raise ValueError("Library or trash settings changed. Create a fresh preview.")
        tracks = [item for item in json.loads(row["tracks"]) if item["status"] == "ready"]
        if not tracks:
            raise ValueError("This preview has no files ready to trash.")
        trashed, errors = [], []
        if outcome:
            for snapshot in json.loads(row["tracks"]):
                outcome({**snapshot, "status": "pending" if snapshot["status"] == "ready" else "skipped",
                         "reason": snapshot["reason"] or "Awaiting recoverable trash move."})
        for index, snapshot in enumerate(tracks):
            if progress:
                progress("Moving reviewed files to recoverable trash", index, len(tracks), snapshot["path"])
            try:
                self._move(snapshot)
                trashed.append({
                    "track_id": snapshot["track_id"], "path": snapshot["path"],
                    "target": snapshot["target"],
                })
                try:
                    self.db.update_track_columns(snapshot["track_id"], status="deleted")
                except sqlite3.Error as exc:
                    raise ValueError(
                        f"File is recoverable at {snapshot['target']}, "
                        f"but catalog update failed: {exc}. Run Scan."
                    ) from exc
                if outcome:
                    outcome({**snapshot, "status": "completed", "reason": "Moved to recoverable trash."})
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append({
                    "track_id": snapshot["track_id"], "path": snapshot["path"],
                    "target": snapshot["target"], "error": str(exc),
                })
                if outcome:
                    outcome({**snapshot, "status": "failed", "error": str(exc)})
        if progress:
            progress("Trash complete", len(tracks), len(tracks), "")
        return {"trashed": trashed, "errors": errors}
