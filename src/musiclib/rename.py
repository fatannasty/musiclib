from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Config
from .db import Database
from .fsops import case_insensitive_exists, remove_empty_dirs
from .models import Track

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

    def plan(self, tracks: Optional[List[Track]] = None) -> RenameReport:
        tracks = tracks or self.db.all_tracks()
        tracks = [t for t in tracks if t.status in ("ok", "suspect")]
        report = RenameReport()
        for track in tracks:
            source = str(Path(track.path).resolve())
            if not os.path.exists(source):
                report.skipped.append((track.path, "file missing on disk"))
                self.db.update_track_columns(track.id, status="deleted")
                continue
            if self._is_library_root_track(source):
                target = str(Path(self.singles_target_path(track)).resolve())
            elif not track.title or not track.album:
                report.skipped.append(
                    (track.path, "missing album/title tags; run musiclib art or fix tags")
                )
                continue
            else:
                target = str(Path(self.target_path(track)).resolve())
            if self._same_file(source, target):
                if source != track.path:
                    self._repath(track.id, source)
                report.already_ok += 1
                continue
            report.moves.append(MovePlan(track, source, target))
        return report

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
