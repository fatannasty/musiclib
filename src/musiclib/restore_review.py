from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .fsops import case_insensitive_exists, is_inside, move_no_replace
from .scanner import Scanner, file_hash

logger = logging.getLogger(__name__)


class RestoreReview:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.library = str(Path(config.library).expanduser().resolve())
        self.trash_root = str(Path(config.trash_dir).expanduser().resolve())
        if is_inside(self.library, self.trash_root):
            raise ValueError("The trash folder must not be the library or contain the library.")
        db.execute(
            "CREATE TABLE IF NOT EXISTS restore_reviews "
            "(id TEXT PRIMARY KEY, library TEXT NOT NULL, trash_root TEXT NOT NULL, "
            "entries TEXT NOT NULL, status TEXT NOT NULL)"
        )

    def _entry(self, deletion_id: int) -> Dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM deletions WHERE id=? AND restored_at IS NULL AND moved_to IS NOT NULL",
            (deletion_id,),
        )
        if row is None:
            raise ValueError("Recoverable trash entry is unavailable or already restored.")
        return dict(row)

    def _paths(self, entry: Dict[str, Any]) -> Tuple[str, str]:
        source = Path(entry["moved_to"])
        target = Path(entry["path"])
        if source.is_symlink() or target.is_symlink():
            raise ValueError("Symbolic-link sources or original paths cannot be restored here.")
        if not is_inside(str(source), self.trash_root) or not is_inside(str(target), self.library):
            raise ValueError("Trash entry is outside the configured trash folder or library.")
        if is_inside(str(target), self.trash_root):
            raise ValueError("Original path is inside trash, not the active library.")
        if not source.is_file():
            raise ValueError("Trashed file is missing from disk.")
        return str(source.resolve()), str(target.resolve())

    def _target_problem(self, source: str, target: str) -> str:
        path = Path(target)
        if not is_inside(target, self.library) or str(path.resolve()) != target:
            return "Original path changed or is outside the configured library."
        if os.path.lexists(target) or case_insensitive_exists(path.parent, path.name):
            return "Original path is occupied (including case-only conflicts). Nothing will be overwritten."
        track = self.db.find_track_by_path(target)
        if track is not None and track.status != "deleted":
            return "Original path belongs to an active catalog entry. Run Scan before restoring."
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            return f"Original parent is not a folder: {parent}"
        if not os.access(Path(source).parent, os.W_OK | os.X_OK):
            return "Cannot remove from the trash folder. Check NAS permissions and PUID/PGID."
        if not os.access(parent, os.W_OK | os.X_OK):
            return "Cannot write to the original folder. Check NAS permissions and PUID/PGID."
        return ""

    def preview(
        self, deletion_ids: List[int], progress: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        if not 1 <= len(deletion_ids) <= 200 or len(set(deletion_ids)) != len(deletion_ids):
            raise ValueError("Select between 1 and 200 distinct trash entries.")
        snapshots = []
        for index, deletion_id in enumerate(deletion_ids):
            if progress:
                progress("Checking selected restore paths", index, len(deletion_ids), str(deletion_id))
            snapshot: Dict[str, Any] = {"deletion_id": deletion_id, "status": "blocked", "reason": ""}
            try:
                entry = self._entry(deletion_id)
                snapshot.update(path=entry["path"], moved_to=entry["moved_to"])
                source, target = self._paths(entry)
                digest = file_hash(source)
                if entry["file_hash"] and entry["file_hash"] != digest:
                    raise ValueError("Trashed file differs from its recorded deletion. Review it before restoring.")
                snapshot.update(
                    source=source, target=target, file_hash=digest,
                    recorded_hash=entry["file_hash"], size=os.stat(source).st_size,
                )
                reason = self._target_problem(source, target)
                if reason:
                    raise ValueError(reason)
                snapshot["status"] = "ready"
            except (OSError, ValueError) as exc:
                snapshot["reason"] = str(exc)
            snapshots.append(snapshot)
        for key in ("source", "target"):
            counts: Dict[str, int] = {}
            for snapshot in snapshots:
                if snapshot.get(key):
                    name = snapshot[key].casefold()
                    counts[name] = counts.get(name, 0) + 1
            for snapshot in snapshots:
                if snapshot.get(key) and counts[snapshot[key].casefold()] > 1:
                    snapshot.update(
                        status="blocked",
                        reason="Selected entries share a trash file or original path. Restore one version at a time.",
                    )
        review_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO restore_reviews (id,library,trash_root,entries,status) VALUES (?,?,?,?,?)",
            (review_id, self.library, self.trash_root, json.dumps(snapshots), "review"),
        )
        if progress:
            progress("Checking selected restore paths", len(deletion_ids), len(deletion_ids), "")
        return {
            "review_id": review_id, "entries": snapshots,
            "ready": sum(item["status"] == "ready" for item in snapshots),
            "blocked": sum(item["status"] == "blocked" for item in snapshots),
            "bytes": sum(item["size"] for item in snapshots if item["status"] == "ready"),
        }

    def status(self, review_id: str) -> str:
        row = self.db.query_one("SELECT status FROM restore_reviews WHERE id=?", (review_id,))
        if row is None:
            raise ValueError("Restore preview was not found.")
        return row["status"]

    def _check_snapshot(self, snapshot: Dict[str, Any]) -> None:
        entry = self._entry(snapshot["deletion_id"])
        source, target = self._paths(entry)
        if (
            entry["path"] != snapshot["path"] or entry["moved_to"] != snapshot["moved_to"]
            or entry["file_hash"] != snapshot["recorded_hash"]
            or (source, target) != (snapshot["source"], snapshot["target"])
            or file_hash(source) != snapshot["file_hash"]
        ):
            raise ValueError("Trash file or recovery entry changed since preview. Create a fresh preview.")

    def apply(
        self, review_id: str, progress: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        row = self.db.query_one("SELECT * FROM restore_reviews WHERE id=?", (review_id,))
        if row is None or row["status"] != "review":
            raise ValueError("This restore preview is unavailable or already used. Create a fresh preview.")
        self.db.execute("UPDATE restore_reviews SET status='used' WHERE id=?", (review_id,))
        if (row["library"], row["trash_root"]) != (self.library, self.trash_root):
            raise ValueError("Library or trash settings changed. Create a fresh preview.")
        entries = [item for item in json.loads(row["entries"]) if item["status"] == "ready"]
        if not entries:
            raise ValueError("This preview has no files ready to restore.")
        restored, errors, restored_paths = [], [], []
        for index, snapshot in enumerate(entries):
            if progress:
                progress("Restoring reviewed files", index, len(entries), snapshot["path"])
            try:
                self._check_snapshot(snapshot)
                reason = self._target_problem(snapshot["source"], snapshot["target"])
                if reason:
                    raise ValueError(reason)

                def verify(target: str, expected: Dict[str, Any] = snapshot) -> None:
                    if not is_inside(target, self.library) or str(Path(target).resolve()) != target:
                        raise ValueError("Original path changed. The trash source was not removed.")
                    if file_hash(target) != expected["file_hash"]:
                        raise ValueError("Restored copy differs from preview. The trash source was not removed.")
                    self._check_snapshot(expected)

                move_no_replace(snapshot["source"], snapshot["target"], verify=verify)
                restored.append({"deletion_id": snapshot["deletion_id"], "path": snapshot["target"]})
                restored_paths.append(snapshot["target"])
                try:
                    self.db.mark_restored(snapshot["deletion_id"])
                    track = self.db.find_track_by_path(snapshot["target"])
                    if track is not None:
                        self.db.update_track_columns(track.id, status="suspect", path=snapshot["target"])
                except sqlite3.Error as exc:
                    raise ValueError(
                        f"File restored to {snapshot['target']}, but catalog/history update failed: {exc}. Run Scan."
                    ) from exc
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append({
                    "deletion_id": snapshot["deletion_id"], "path": snapshot["path"],
                    "source": snapshot["source"], "error": str(exc),
                })
        scan_errors = []
        if restored_paths:
            try:
                scan_report = Scanner(self.config, self.db).refresh_paths(
                    restored_paths, progress_state=progress,
                )
                scan_errors = [
                    {
                        "path": path,
                        "error": "File was restored, but its catalog metadata could not be refreshed. Run Scan.",
                    }
                    for path in scan_report.error_paths
                ]
            except Exception as exc:
                logger.exception("Could not refresh catalog metadata after reviewed restore")
                scan_errors = [{"path": path, "error": str(exc)} for path in restored_paths]
        if progress:
            progress("Restore complete", len(entries), len(entries), "")
        return {"restored": restored, "errors": errors, "scan_errors": scan_errors}
