from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import mutagen

from .config import Config
from .db import Database
from .models import Album, Issue, Track, audio_format

FOLDER_IMAGE_NAMES = [
    "folder.jpg", "folder.png", "cover.jpg", "cover.png", "front.jpg", "front.png",
    "album.jpg", "album.png", "art.jpg", "art.png", "thumb.jpg",
]

ART_KEYS = {
    "mp3": ("APIC",),
    "flac": ("__picture",),
    "mp4": ("covr",),
    "alac": ("covr",),
    "ogg": ("metadata_block_picture",),
    "opus": ("metadata_block_picture",),
    "wma": ("WM/Picture",),
    "aiff": ("APIC",),
    "wav": ("APIC",),
}

TRACKNUM_RE = re.compile(r"^\s*(\d+)\s*(?:/\s*(\d+))?\s*$")
YEAR_RE = re.compile(r"(\d{4})")


def _first_tag(tags: Dict, keys: tuple) -> Optional[str]:
    for key in keys:
        value = tags.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_pair(value: Optional[str]) -> tuple:
    if not value:
        return (None, None)
    match = TRACKNUM_RE.match(str(value))
    if not match:
        return (None, None)
    first = int(match.group(1))
    second = int(match.group(2)) if match.group(2) else None
    return (first, second)


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _has_embedded_art(path: str, fmt: str, audio: mutagen.File) -> bool:
    keys = ART_KEYS.get(fmt, ())
    if fmt == "flac":
        return bool(audio.pictures)
    tags = audio.tags or {}
    for key in keys:
        if key == "APIC" and hasattr(tags, "getall"):
            if tags.getall("APIC"):
                return True
        elif key in tags:
            return True
    return False


def extract_tags(path: str) -> Dict:
    fmt = audio_format(path)
    audio = mutagen.File(path, easy=False)
    if audio is None:
        return {"path": path, "format": fmt, "error": "unparseable"}
    try:
        easy_audio = mutagen.File(path, easy=True)
        easy = dict(easy_audio.tags or {})
    except Exception:
        easy = {}
    raw = {}
    try:
        if audio.tags:
            raw = dict(audio.tags)
    except Exception:
        raw = {}
    artist = _first_tag(easy, ("artist",)) or _first_tag(
        raw, ("artist", "ARTIST", "TPE1", "Author", "\xa9ART", "author")
    )
    album_artist = _first_tag(easy, ("albumartist",)) or _first_tag(
        raw, ("albumartist", "ALBUMARTIST", "album artist", "TPE2", "aART",
              "Album Artist", "WM/AlbumArtist")
    )
    album = _first_tag(easy, ("album",)) or _first_tag(
        raw, ("album", "ALBUM", "TALB", "\xa9alb", "WM/AlbumTitle")
    )
    title = _first_tag(easy, ("title",)) or _first_tag(
        raw, ("title", "TITLE", "TIT2", "\xa9nam", "TrackTitle", "Name")
    )
    genre = _first_tag(easy, ("genre",)) or _first_tag(
        raw, ("genre", "GENRE", "TCON", "\xa9gen", "WM/Genre")
    )
    track_no, track_total = _parse_pair(
        _first_tag(easy, ("tracknumber",))
        or _first_tag(
            raw, ("tracknumber", "TRCK", "track", "TRK", "trkn", "WM/TrackNumber", "\xa9trk")
        )
    )
    disc_no, disc_total = _parse_pair(
        _first_tag(easy, ("discnumber",))
        or _first_tag(
            raw, ("discnumber", "TPOS", "disc", "TPS", "disk", "WM/PartOfSet")
        )
    )
    date = _first_tag(easy, ("date", "originaldate")) or _first_tag(
        raw, ("date", "DATE", "year", "YEAR", "TDRC", "\xa9day", "WM/Year")
    )
    year = None
    if date:
        match = YEAR_RE.search(str(date))
        if match:
            year = int(match.group(1))
    mbid_track = _first_tag(easy, ("musicbrainz_trackid",)) or _first_tag(
        raw, ("musicbrainz_trackid", "MusicBrainz Track Id", "MusicBrainz/TRACK ID")
    )
    mbid_release = _first_tag(easy, ("musicbrainz_albumid",)) or _first_tag(
        raw, ("musicbrainz_albumid", "MusicBrainz Album Id", "MusicBrainz/ALBUM ID")
    )
    info = audio.info
    bitrate = getattr(info, "bitrate", None)
    if bitrate is not None and bitrate > 0:
        bitrate = int(bitrate / 1000) if bitrate > 2000 else int(bitrate)
    return {
        "path": path,
        "format": fmt,
        "error": None,
        "bitrate": bitrate,
        "sample_rate": getattr(info, "sample_rate", None),
        "bit_depth": getattr(info, "bits_per_sample", None),
        "channels": getattr(info, "channels", None),
        "duration": getattr(info, "length", None),
        "artist": artist,
        "album_artist": album_artist or artist,
        "album": album,
        "title": title,
        "genre": genre,
        "track_no": _to_int(track_no) if not isinstance(track_no, int) else track_no,
        "track_total": _to_int(track_total) if not isinstance(track_total, int) else track_total,
        "disc_no": disc_no,
        "disc_total": disc_total,
        "year": year,
        "mbid_track": mbid_track,
        "mbid_release": mbid_release,
        "has_embedded_art": _has_embedded_art(path, fmt or "", audio),
    }


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_worker(path: str) -> Dict:
    try:
        stat = os.stat(path)
    except OSError as exc:
        return {"path": path, "size": 0, "mtime": 0.0, "error": f"{type(exc).__name__}: {exc}"}
    result = None
    try:
        result = extract_tags(path)
    except Exception as exc:
        result = {
            "path": path,
            "format": audio_format(path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    result["size"] = stat.st_size
    result["mtime"] = stat.st_mtime
    if not result.get("error"):
        try:
            result["file_hash"] = file_hash(path)
        except OSError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


@dataclass
class ScanReport:
    scanned: int = 0
    skipped_unchanged: int = 0
    added: int = 0
    updated: int = 0
    errors: int = 0
    error_paths: List[str] = field(default_factory=list)
    removed: int = 0


class Scanner:
    def __init__(self, config: Config, db: Database, workers: Optional[int] = None):
        self.config = config
        self.db = db
        self.workers = workers or max(4, (os.cpu_count() or 4) - 1)

    def iter_audio_files(self, root: Union[str, Path]) -> List[str]:
        files: List[str] = []
        trash = str(Path(self.config.trash_dir).resolve())
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and str(Path(dirpath, d).resolve()) != trash
            ]
            for name in filenames:
                if name.startswith("._") or name == ".DS_Store":
                    continue
                if audio_format(name):
                    files.append(os.path.join(dirpath, name))
        return files

    def scan(
        self,
        root: Optional[Union[str, Path]] = None,
        rescan: bool = False,
        progress: Optional[Callable[[int, str], None]] = None,
        progress_state: Optional[Callable[[int, int, str], None]] = None,
    ) -> ScanReport:
        root = str(Path(root or self.config.library).expanduser())
        report = ScanReport()
        paths = self.iter_audio_files(root)
        report.scanned = len(paths)
        existing = {
            row["path"]: row
            for row in self.db.query(
                "SELECT path, size, mtime, file_hash, status FROM tracks"
            )
        }
        known_paths = set(existing)
        pending: List[str] = []
        for path in paths:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            row = existing.get(path)
            if not rescan and row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
                report.skipped_unchanged += 1
                continue
            pending.append(path)

        done = 0
        if progress_state:
            progress_state(0, len(pending), "")
        if pending:
            with ProcessPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(scan_worker, p): p for p in pending}
                for future in as_completed(futures):
                    result = future.result()
                    done += 1
                    self._store_result(result, report, row_exists=result["path"] in known_paths)
                    if progress:
                        progress(done, result["path"])
                    if progress_state:
                        progress_state(done, len(pending), result["path"])

        for path in known_paths - set(paths):
            if path.startswith(root) and existing[path]["status"] != "deleted":
                self.db.execute(
                    "UPDATE tracks SET status='deleted' WHERE path=?", (path,)
                )
                report.removed += 1
        self.rebuild_albums(root)
        return report

    def _store_result(
        self, result: Dict, report: ScanReport, row_exists: bool, preserve_health: bool = False,
    ) -> None:
        path = result["path"]
        track = self.db.find_track_by_path(path)
        if result.get("error"):
            if track is None:
                track = Track(path=path)
            track.issues = [Issue("unparseable", result["error"], "bad")]
            track.status = "bad"
            track.size = result.get("size", 0)
            track.mtime = result.get("mtime", 0.0)
            self.db.upsert_track(track)
            report.errors += 1
            if len(report.error_paths) < 50:
                report.error_paths.append(path)
            return
        if track is None:
            track = Track(path=path)
            report.added += 1
        else:
            report.updated += 1
        track.mtime = result["mtime"]
        track.size = result["size"]
        track.format = result["format"]
        track.bitrate = result["bitrate"]
        track.sample_rate = result["sample_rate"]
        track.bit_depth = result["bit_depth"]
        track.channels = result["channels"]
        track.duration = result["duration"]
        track.artist = result["artist"]
        track.album_artist = result["album_artist"]
        track.album = result["album"]
        track.title = result["title"]
        track.genre = result["genre"]
        track.track_no = result["track_no"]
        track.track_total = result["track_total"]
        track.disc_no = result["disc_no"]
        track.disc_total = result["disc_total"]
        track.year = result["year"]
        track.mbid_track = result["mbid_track"]
        track.mbid_release = result["mbid_release"]
        track.has_embedded_art = result["has_embedded_art"]
        if track.file_hash != result["file_hash"]:
            track.fingerprint = None
        track.file_hash = result["file_hash"]
        issues: List[Issue] = (
            [issue for issue in track.issues if issue.kind != "missing_tags"]
            if preserve_health else []
        )
        if not result["title"] or not result["album"]:
            issues.append(Issue("missing_tags", "missing album or title tag", "warn"))
        track.issues = issues
        track.status = "bad" if any(issue.severity == "bad" for issue in issues) else (
            "suspect" if issues else "ok"
        )
        self.db.upsert_track(track)

    def rebuild_albums(self, root: Optional[str] = None) -> None:
        root = str(Path(root or self.config.library).expanduser())
        rows = self.db.query(
            """
            SELECT album_artist, album, MIN(year) AS year,
                   COUNT(*) AS have_tracks,
                   MAX(has_embedded_art) AS any_art,
                   MAX(mbid_release) AS mbid_release
            FROM tracks
            WHERE status IN ('ok', 'suspect') AND album IS NOT NULL AND album != ''
            GROUP BY LOWER(album_artist), LOWER(album)
            """
        )
        folder_images = self._find_folder_images(root)
        for row in rows:
            album = Album(
                album_artist=row["album_artist"] or "Unknown Artist",
                album=row["album"],
                year=row["year"],
                mbid_release=row["mbid_release"],
                have_tracks=row["have_tracks"],
            )
            album_dir = self._album_dir(row["album_artist"], row["album"], root)
            has_folder_art = album_dir in folder_images
            album.art_status = "ok" if (row["any_art"] or has_folder_art) else "missing"
            self.db.upsert_album(album)

    def _find_folder_images(self, root: str) -> set:
        found = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in FOLDER_IMAGE_NAMES:
                if name in filenames:
                    found.add(dirpath)
        return found

    def _album_dir(self, album_artist: Optional[str], album: str, root: str) -> str:
        if not album_artist:
            return ""
        rows = self.db.query(
            "SELECT path FROM tracks WHERE LOWER(album_artist)=LOWER(?) "
            "AND LOWER(album)=LOWER(?) AND status IN ('ok','suspect') LIMIT 1",
            (album_artist, album),
        )
        if not rows:
            return ""
        return str(Path(rows[0]["path"]).parent)
