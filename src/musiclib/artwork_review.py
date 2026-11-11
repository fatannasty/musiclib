from __future__ import annotations

import base64
import io
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import mutagen
from PIL import Image, UnidentifiedImageError

from .artwork import embed_cover, image_mime, read_cover
from .config import Config
from .db import Database, utcnow
from .fsops import atomic_edit, is_inside
from .models import Track
from .scanner import file_hash, scan_worker

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BASE64 = ((MAX_IMAGE_BYTES + 2) // 3) * 4
MAX_IMAGE_PIXELS = 25_000_000
SUPPORTED_FORMATS = {"mp3", "flac", "mp4", "alac", "ogg", "opus", "wav", "aiff"}


def validate_cover(encoded: str) -> Tuple[bytes, str, int, int]:
    if len(encoded) > MAX_IMAGE_BASE64:
        raise ValueError("Choose a JPEG or PNG image no larger than 8 MiB.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("The uploaded image is not valid base64.") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Choose a JPEG or PNG image no larger than 8 MiB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG"):
                raise ValueError("Choose a JPEG or PNG image.")
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Cover images must contain at most 25 million pixels.")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Choose a still image, not animated artwork.")
            mime = "image/png" if image.format == "PNG" else "image/jpeg"
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("The uploaded image is damaged or cannot be decoded.") from exc
    return data, mime, width, height


def write_cover(path: Path, data: bytes, expected_hash: str) -> Dict[str, Any]:
    def edit(temporary: Path) -> Dict[str, Any]:
        embed_cover(str(temporary), data)
        result = scan_worker(str(temporary))
        if result.get("error"):
            raise ValueError(f"Updated file could not be read: {result['error']}")
        if not result.get("has_embedded_art"):
            raise ValueError("The updated file did not retain embedded artwork.")
        if read_cover(str(temporary), include_folder=False) != (data, image_mime(data)):
            raise ValueError("The saved cover could not be verified. The original file was not replaced.")
        if path.is_symlink() or file_hash(str(path)) != expected_hash:
            raise ValueError("File changed while preparing artwork. The original file was not replaced.")
        result["path"] = str(path)
        return result

    return atomic_edit(path, edit)


class ArtworkReview:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS artwork_reviews ("
            "id TEXT PRIMARY KEY, image BLOB NOT NULL, mime TEXT NOT NULL, "
            "tracks TEXT NOT NULL, status TEXT NOT NULL)"
        )

    def _check_source(self, track: Optional[Track]) -> Track:
        if track is None or track.status == "deleted":
            raise ValueError("Track is not available in the library.")
        path = Path(track.path)
        if not is_inside(track.path, self.config.library):
            raise ValueError("Track is outside the configured library.")
        if path.is_symlink():
            raise ValueError("Symbolic links cannot be edited.")
        if not path.is_file():
            raise ValueError("Track file is missing from disk.")
        if track.format not in SUPPORTED_FORMATS:
            raise ValueError("This audio format does not support reviewed artwork embedding.")
        if not track.file_hash or file_hash(track.path) != track.file_hash:
            raise ValueError("File changed since its last scan. Rescan before previewing artwork.")
        return track

    def preview(
        self, track_ids: List[int], encoded: str, filename: str = "Uploaded cover",
        progress: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise ValueError("Select between 1 and 200 distinct tracks.")
        data, mime, width, height = validate_cover(encoded)
        tracks = []
        seen = set()
        for index, track_id in enumerate(track_ids):
            track = self.db.get_track(track_id)
            item = {
                "track_id": track_id, "path": track.path if track else None,
                "title": track.title if track else None, "file_hash": track.file_hash if track else None,
                "has_embedded_art": bool(track and track.has_embedded_art),
                "status": "blocked", "reason": "",
            }
            if progress:
                progress("Checking selected artwork targets", index, len(track_ids), item["path"] or "")
            try:
                track = self._check_source(track)
                canonical = str(Path(track.path).resolve())
                if canonical in seen:
                    raise ValueError("This file is already included in the preview.")
                seen.add(canonical)
                item["has_embedded_art"] = read_cover(track.path, include_folder=False) is not None
                item["status"] = "ready"
            except (ValueError, OSError) as exc:
                item["reason"] = str(exc)
            tracks.append(item)
        review_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO artwork_reviews (id, image, mime, tracks, status) VALUES (?,?,?,?,?)",
            (review_id, data, mime, json.dumps(tracks), "review"),
        )
        warnings = []
        if min(width, height) < int(self.config.artwork.get("min_side", 300)):
            warnings.append("This image is smaller than the preferred minimum side in Settings.")
        if progress:
            progress("Artwork preview ready", len(track_ids), len(track_ids))
        return {
            "review_id": review_id, "cover_url": f"/api/artwork/reviews/{review_id}/cover",
            "filename": filename, "mime": mime, "width": width, "height": height,
            "bytes": len(data), "ready": sum(track["status"] == "ready" for track in tracks),
            "blocked": sum(track["status"] == "blocked" for track in tracks),
            "warnings": warnings, "tracks": tracks,
        }

    def status(self, review_id: str) -> str:
        row = self.db.query_one("SELECT status FROM artwork_reviews WHERE id=?", (review_id,))
        if row is None:
            raise ValueError("Artwork preview was not found.")
        return row["status"]

    def cover(self, review_id: str) -> Tuple[bytes, str]:
        row = self.db.query_one("SELECT image, mime FROM artwork_reviews WHERE id=?", (review_id,))
        if row is None:
            raise ValueError("Artwork preview was not found.")
        return bytes(row["image"]), row["mime"]

    def apply(
        self, review_id: str, progress: Optional[Callable[..., None]] = None,
        outcome: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        row = self.db.query_one("SELECT * FROM artwork_reviews WHERE id=?", (review_id,))
        if row is None or row["status"] != "review":
            raise ValueError("This artwork preview is unavailable or already used. Create a fresh preview.")
        tracks = [track for track in json.loads(row["tracks"]) if track["status"] == "ready"]
        if not tracks:
            raise ValueError("This preview has no files ready for artwork.")
        self.db.execute("UPDATE artwork_reviews SET status='used' WHERE id=?", (review_id,))
        data = bytes(row["image"])
        applied, errors = [], []
        if outcome:
            for snapshot in json.loads(row["tracks"]):
                outcome({**snapshot, "status": "pending" if snapshot["status"] == "ready" else "skipped",
                         "reason": snapshot["reason"] or "Awaiting artwork update."})
        for index, snapshot in enumerate(tracks):
            track_id = snapshot["track_id"]
            if progress:
                progress("Embedding reviewed artwork", index, len(tracks), snapshot["path"])
            try:
                track = self._check_source(self.db.get_track(track_id))
                if (track.path, track.file_hash) != (snapshot["path"], snapshot["file_hash"]):
                    raise ValueError("File moved or changed since artwork preview. Create a fresh preview.")
                result = write_cover(Path(track.path), data, snapshot["file_hash"])
                self.db.update_track_columns(
                    track_id, size=result["size"], mtime=result["mtime"],
                    has_embedded_art=1, file_hash=result["file_hash"],
                    fingerprint=None, scanned_at=utcnow(),
                )
                self.db.execute(
                    "UPDATE albums SET art_status='ok' "
                    "WHERE LOWER(album_artist)=LOWER(?) AND LOWER(album)=LOWER(?)",
                    (track.album_artist or track.artist or "Unknown Artist", track.album or ""),
                )
                applied.append({"track_id": track_id, "path": track.path})
                if outcome:
                    outcome({**snapshot, "status": "completed", "reason": "Embedded reviewed artwork."})
            except (ValueError, OSError, mutagen.MutagenError) as exc:
                errors.append({"track_id": track_id, "path": snapshot["path"], "error": str(exc)})
                if outcome:
                    outcome({**snapshot, "status": "failed", "error": str(exc)})
        if progress:
            progress("Artwork update complete", len(tracks), len(tracks))
        return {"applied": applied, "errors": errors}
