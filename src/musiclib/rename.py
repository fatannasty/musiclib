from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .fsops import case_insensitive_exists, is_inside, move_no_replace, remove_empty_dirs
from .models import Track
from .scanner import file_hash

FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
CLEANUP_RE = re.compile(r"\(\s*\)|\[\s*\]")
SPACE_RE = re.compile(r"  +")


def sanitize_component(text: str, max_bytes: int = 120) -> str:
    text = FORBIDDEN_RE.sub("_", text)
    text = CLEANUP_RE.sub("", text)
    text = SPACE_RE.sub(" ", text).strip(" .-_")
    encoded = text.encode("utf-8")
    while len(encoded) > max_bytes:
        text = text[:-1]
        encoded = text.encode("utf-8")
    return text or "_"


class _TemplateMap(dict):
    def __missing__(self, key: str) -> str:
        if key.startswith("track") or key.startswith("disc"):
            return "0"
        return ""


@dataclass
class MovePlan:
    track: Track
    source: str
    target: str
    status: str = "planned"
    reason: str = ""


@dataclass
class RenameReport:
    moves: List[MovePlan] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)
    moved: int = 0
    already_ok: int = 0


class Renamer:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.library = str(Path(config.library).expanduser().resolve())
        self.template = config.organize.get(
            "template",
            "{albumartist}/{album} ({year})/{track:02d} - {title}.{ext}",
        )
        self.template_multidisc = config.organize.get(
            "template_multidisc",
            "{albumartist}/{album} ({year})/CD{disc}/{track:02d} - {title}.{ext}",
        )

    def target_path(self, track: Track) -> str:
        album_artist = track.album_artist or track.artist or "Unknown Artist"
        artist = track.artist or album_artist
        album = track.album or "Unknown Album"
        title = track.title or "Unknown Title"
        year = str(track.year) if track.year else ""
        track_no = track.track_no or 0
        disc_no = track.disc_no or (1 if (track.disc_total or 1) > 1 else 0)
        multidisc = (track.disc_total or 1) > 1 or (track.disc_no or 0) > 1
        template = self.template_multidisc if multidisc else self.template
        ext = Path(track.path).suffix.lstrip(".")
        values = _TemplateMap(
            albumartist=album_artist,
            artist=artist,
            album=album,
            title=title,
            year=year,
            track=track_no,
            disc=disc_no,
            disc_total=track.disc_total or 1,
            track_total=track.track_total or 0,
            genre=track.genre or "",
            ext=ext,
        )
        relative = template.format_map(values)
        parts = [sanitize_component(p) for p in relative.split("/") if p]
        parts = [p for p in parts if p and p != "_"]
        result = os.path.join(self.library, *parts)
        return result

    def singles_target_path(self, track: Track) -> str:
        genre = sanitize_component(track.genre or "Unknown Genre")
        folder = sanitize_component(self.config.organize.get("singles_folder", "Singles"))
        artist = sanitize_component(track.artist or track.album_artist or "Unknown Artist")
        title = sanitize_component(track.title or Path(track.path).stem)
        ext = Path(track.path).suffix
        filename = f"{artist} - {title}{ext}"
        if not track.title and title.startswith(artist + " - "):
            filename = title + ext
        return os.path.join(self.library, folder, genre, filename)

    def _is_library_root_track(self, source: str) -> bool:
        if not self.config.organize.get("root_singles", True):
            return False
        path = Path(source)
        if path.parent == Path(self.library):
            return True
        folder = sanitize_component(self.config.organize.get("singles_folder", "Singles"))
        try:
            relative = path.relative_to(self.library)
        except ValueError:
            return False
        return len(relative.parts) == 3 and relative.parts[0] == folder

    def plan(
        self, tracks: Optional[List[Track]] = None, album_mode: bool = False,
    ) -> RenameReport:
        tracks = self.db.all_tracks() if tracks is None else tracks
        tracks = [t for t in tracks if t.status in ("ok", "suspect")]
        report = RenameReport()
        for track in tracks:
            source = str(Path(track.path).resolve())
            if not os.path.exists(source):
                report.skipped.append((track.path, "file missing on disk"))
                continue
            if not album_mode and self._is_library_root_track(source):
                target = str(Path(self.singles_target_path(track)).resolve())
            elif not track.title or not track.album:
                report.skipped.append(
                    (track.path, "missing album/title tags; run musiclib art or fix tags")
                )
                continue
            else:
                target = str(Path(self.target_path(track)).resolve())
            if self._same_file(source, target):
                report.already_ok += 1
                continue
            report.moves.append(MovePlan(track, source, target))
        return report

    def preview_selected(
        self, track_ids: List[int],
        progress: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise ValueError("Select between 1 and 200 distinct tracks.")
        tracks, skipped = [], []
        for track_id in track_ids:
            track = self.db.get_track(track_id)
            if track is None or track.status not in ("ok", "suspect"):
                skipped.append({"track_id": track_id, "reason": "Track is unavailable or marked bad."})
            elif not is_inside(track.path, self.library) or Path(track.path).is_symlink():
                skipped.append({
                    "track_id": track_id, "path": track.path,
                    "reason": "File is outside the library or is a symbolic link.",
                })
            else:
                tracks.append(track)
        report = self.plan(tracks, album_mode=True)
        skipped.extend({"path": path, "reason": reason} for path, reason in report.skipped)
        targets: Dict[str, int] = {}
        for move in report.moves:
            key = move.target.casefold()
            targets[key] = targets.get(key, 0) + 1
        moves = []
        for index, move in enumerate(report.moves, start=1):
            if progress:
                progress("Checking selected rename targets", index - 1, len(report.moves), move.source)
            reason = ""
            try:
                reason = self._review_problem(move.source, move.target, move.track.id)
                if targets[move.target.casefold()] > 1:
                    reason = "Multiple selected files have the same target. Fix their titles or numbers."
                if not move.track.file_hash or file_hash(move.source) != move.track.file_hash:
                    reason = "File changed since the last scan or has no hash. Scan it before renaming."
            except OSError as exc:
                reason = str(exc)
            moves.append({
                "track_id": move.track.id, "path": move.track.path,
                "source": move.source, "target": move.target,
                "file_hash": move.track.file_hash,
                "status": "blocked" if reason else "ready", "reason": reason,
            })
            if progress:
                progress("Checking selected rename targets", index, len(report.moves), move.source)
        return {
            "moves": moves, "skipped_files": skipped, "already_ok": report.already_ok,
            "ready": sum(move["status"] == "ready" for move in moves),
            "blocked": sum(move["status"] == "blocked" for move in moves),
            "templates": {"album": self.template, "multidisc": self.template_multidisc},
        }

    def apply_selected(
        self, preview: Dict[str, Any],
        progress: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        if preview["templates"] != {"album": self.template, "multidisc": self.template_multidisc}:
            raise ValueError("Organize templates changed. Create a new rename preview.")
        moves = [dict(move) for move in preview["moves"] if move["status"] == "ready"]
        errors, moved = [], 0
        for index, move in enumerate(moves, start=1):
            if progress:
                progress("Renaming reviewed files", index - 1, len(moves), move["source"])
            try:
                track = self.db.get_track(move["track_id"])
                if (
                    track is None or track.status not in ("ok", "suspect")
                    or track.path != move["path"] or track.file_hash != move["file_hash"]
                    or Path(track.path).is_symlink()
                    or str(Path(track.path).resolve()) != move["source"]
                    or str(Path(self.target_path(track)).resolve()) != move["target"]
                ):
                    raise ValueError("Track changed since preview. Create a new preview.")
                reason = self._review_problem(move["source"], move["target"], track.id)
                if reason:
                    raise ValueError(reason)
                if file_hash(move["source"]) != move["file_hash"]:
                    raise ValueError("File changed since preview. Scan it and preview again.")
                self._move_no_replace(move["source"], move["target"])
                moved += 1
                try:
                    self._repath(track.id, move["target"])
                except sqlite3.Error as exc:
                    raise ValueError(
                        f"File moved to {move['target']}, but catalog update failed: {exc}. Run Scan."
                    ) from exc
                move.update(status="done", reason="")
            except (OSError, ValueError, sqlite3.Error) as exc:
                move.update(status="error", reason=str(exc))
                errors.append({
                    "track_id": move["track_id"], "source": move["source"],
                    "target": move["target"], "error": str(exc),
                })
            if progress:
                progress("Renaming reviewed files", index, len(moves), move["source"])
        return {
            "moved": moved, "moves": moves, "errors": errors,
            "blocked": preview["blocked"], "already_ok": preview["already_ok"],
            "skipped_files": preview["skipped_files"],
        }

    def _review_problem(self, source: str, target: str, track_id: Optional[int]) -> str:
        if not is_inside(source, self.library) or not is_inside(target, self.library):
            return "Source or target is outside the configured library."
        if not os.path.isfile(source):
            return "Source file is missing from disk."
        if os.path.lexists(target) or case_insensitive_exists(Path(target).parent, Path(target).name):
            return "Target is occupied (including case-only filename conflicts). No file will be overwritten."
        occupant = self.db.query_one(
            "SELECT id FROM tracks WHERE path=? AND status!='deleted' AND id!=?",
            (target, track_id),
        )
        if occupant:
            return "Target belongs to another catalog entry. Run Scan and preview again."
        parent = Path(target).parent
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            return f"Target parent is not a folder: {parent}"
        if not os.access(Path(source).parent, os.W_OK | os.X_OK):
            return "Cannot rename from the source folder. Check NAS folder permissions and PUID/PGID."
        if not os.access(parent, os.W_OK | os.X_OK):
            return "Cannot write to the target folder. Check NAS folder permissions and PUID/PGID."
        return ""

    def _move_no_replace(self, source: str, target: str) -> None:
        move_no_replace(source, target)

    def apply(self, report: Optional[RenameReport] = None) -> RenameReport:
        report = report or self.plan()
        pending = list(report.moves)
        while pending:
            progressed = False
            deferred: List[MovePlan] = []
            for move in pending:
                if os.path.exists(move.target):
                    occupant = self._occupant(move)
                    if occupant and any(m.source == move.target for m in pending + deferred):
                        deferred.append(move)
                        continue
                    move.target = self._bump(move)
                    if self._same_file(move.source, move.target):
                        move.status = "conflict"
                        move.reason = "target occupied"
                        report.errors.append((move.source, "target occupied: " + move.target))
                        continue
                try:
                    self._move(move.source, move.target)
                    self._repath(move.track.id, move.target)
                    move.status = "done"
                    report.moved += 1
                    progressed = True
                except (OSError, sqlite3.Error) as exc:
                    move.status = "error"
                    move.reason = str(exc)
                    report.errors.append((move.source, str(exc)))
                    progressed = True
            if not progressed:
                for move in deferred:
                    move.target = self._bump(move)
                    try:
                        self._move(move.source, move.target)
                        self._repath(move.track.id, move.target)
                        move.status = "done"
                        report.moved += 1
                    except (OSError, sqlite3.Error) as exc:
                        report.errors.append((move.source, str(exc)))
                break
            pending = deferred
        if self.config.organize.get("cleanup_empty_dirs", True):
            remove_empty_dirs(self.library)
        return report

    def _repath(self, track_id: int, target: str) -> None:
        self.db.execute(
            "DELETE FROM tracks WHERE path=? AND status='deleted' AND id != ?",
            (target, track_id),
        )
        self.db.update_track_columns(track_id, path=target)

    def _occupant(self, move: MovePlan) -> bool:
        row = self.db.query_one("SELECT id FROM tracks WHERE path=?", (move.target,))
        return row is not None

    def _bump(self, move: MovePlan) -> str:
        base, ext = os.path.splitext(move.target)
        counter = 2
        while True:
            candidate = f"{base} ({counter}){ext}"
            if not os.path.exists(candidate) and not case_insensitive_exists(
                Path(candidate).parent, Path(candidate).name
            ):
                return candidate
            counter += 1

    def _move(self, source: str, target: str) -> None:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.rename(source, target)
        except OSError:
            if sys.platform == "darwin":
                try:
                    os.rename(source, target)
                    return
                except OSError:
                    pass
            shutil.move(source, target)

    def _same_file(self, source: str, target: str) -> bool:
        if source == target:
            return True
        if not os.path.exists(target):
            return False
        try:
            if os.path.samefile(source, target):
                return True
        except OSError:
            pass
        if source.lower() == target.lower():
            existing = case_insensitive_exists(Path(target).parent, Path(target).name)
            return existing is not None and str(existing) == source
        return False
