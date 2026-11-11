from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from mutagen import MutagenError
from pydantic import BaseModel, Field

from ..artwork import CAA_FRONT_THUMB, Artwork, read_cover
from ..artwork_review import MAX_IMAGE_BASE64, ArtworkReview
from ..brainz import Brainz
from ..config import Config
from ..db import Database
from ..doctor import Doctor
from ..duplicates import DuplicateFinder
from ..fingerprint import compute_all, fpcalc_available
from ..fsops import is_inside, move_no_replace
from ..metadata_review import (
    ALBUM_FIELDS,
    ALBUM_SOURCES,
    EDITABLE_FIELDS,
    TRACK_FIELDS,
    MetadataReview,
)
from ..models import audio_format
from ..rename import Renamer
from ..restore_review import RestoreReview
from ..scanner import Scanner, file_hash
from ..trash_review import TrashReview
from ..usenet import Sabnzbd, SabnzbdError, search_all
from .duplicate_schedule import DuplicateSchedule
from .jobs import JobRegistry
from .playback import MEDIA_TYPES, Playback

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class ApplyBody(BaseModel):
    apply: bool = False
    hard_delete: bool = False
    deep: Optional[bool] = None
    replace_small: bool = False
    strategy: Optional[str] = None
    fingerprint: bool = False


class ScanBody(BaseModel):
    rescan: bool = False


class AlbumTrackSnapshot(BaseModel):
    path: str
    file_hash: str


class KeepOverride(BaseModel):
    hard_delete: bool = False
    keep_ids: Dict[int, int] = {}
    group_ids: Optional[List[int]] = None
    member_ids: Dict[int, List[int]] = {}
    snapshots: Optional[Dict[int, AlbumTrackSnapshot]] = None


class ManualDupeBody(BaseModel):
    track_ids: List[int]
    keep_id: int
    hard_delete: bool = False


class SearchBody(BaseModel):
    query: str
    limit: int = 25


class TrackSearchBody(BaseModel):
    query: str = ""
    limit: int = 100
    offset: int = 0
    genre: str = ""
    format: str = ""
    status: str = ""


class RestoreBody(BaseModel):
    deletion_id: int


class RestorePreviewBody(BaseModel):
    deletion_ids: List[int]


class MetadataLookupBody(BaseModel):
    track_id: int
    title: Optional[str] = None
    artist: Optional[str] = None
    source: Literal["all", "musicbrainz", "itunes", "deezer"] = "all"


class MetadataApplyBody(BaseModel):
    review_id: int
    candidate_index: int
    fields: List[str]


class MetadataIdentifyBody(BaseModel):
    track_id: int


class MetadataBulkLookupBody(BaseModel):
    track_ids: List[int]
    source: Literal["all", "musicbrainz", "itunes", "deezer"] = "all"


class MetadataBulkApplyBody(BaseModel):
    reviews: List[MetadataApplyBody]


class IssuesTrashBody(BaseModel):
    track_ids: List[int]
    hard_delete: bool = False


class AlbumSuggestBody(BaseModel):
    track_ids: List[int]
    album: Optional[str] = None
    artist: Optional[str] = None
    source: Optional[Literal["all", "musicbrainz", "itunes", "deezer", "library"]] = None


class SelectedDuplicatesBody(BaseModel):
    groups: List[ManualDupeBody]
    snapshots: Dict[int, AlbumTrackSnapshot]


class AlbumApplyBody(BaseModel):
    track_ids: List[int]
    fields: List[str]
    values: Dict[str, Any]
    rename: bool = False
    snapshots: Optional[Dict[int, AlbumTrackSnapshot]] = None


class AlbumEditBody(BaseModel):
    track_ids: List[int]
    fields: List[str]
    values: Dict[str, Any]


class TrackEditBody(BaseModel):
    track_id: int
    fields: List[str]
    values: Dict[str, Any]


class NumberingPreviewBody(BaseModel):
    track_ids: List[int]
    start_no: Any = 1
    disc_no: Any = None


class TrackSelectionBody(BaseModel):
    track_ids: List[int]


class ReviewedApplyBody(BaseModel):
    preview_job_id: str


class ArtworkPreviewBody(BaseModel):
    track_ids: List[int]
    image_base64: str = Field(max_length=MAX_IMAGE_BASE64)
    filename: str = Field(default="Uploaded cover", max_length=255)


class SabAddBody(BaseModel):
    url: str
    name: Optional[str] = None
    indexer: str = ""


class SettingsBody(BaseModel):
    organize: Optional[Dict[str, Any]] = None
    doctor: Optional[Dict[str, Any]] = None
    duplicates: Optional[Dict[str, Any]] = None
    artwork: Optional[Dict[str, Any]] = None
    brainz: Optional[Dict[str, Any]] = None
    catalog: Optional[Dict[str, Any]] = None
    acoustid: Optional[Dict[str, Any]] = None
    sabnzbd: Optional[Dict[str, Any]] = None
    indexers: Optional[List[Dict[str, Any]]] = None


EDITABLE_SECTIONS = ("organize", "doctor", "duplicates", "artwork", "brainz", "catalog", "acoustid", "sabnzbd")
SECRET_KEYS = ("api_key",)


def _coerce(value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"expected a number, got {value!r}") from exc
    return "" if value is None else str(value).strip()


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="musiclib", docs_url=None, redoc_url=None)
    db = Database(config.db_path)
    Scanner(config, db).rebuild_albums()

    def refresh_album_summaries() -> None:
        if db.albums_dirty:
            Scanner(config, db).rebuild_albums()

    jobs = JobRegistry(db, after_write=refresh_album_summaries)
    playback = Playback(config, db)

    def sab_client() -> Sabnzbd:
        if not config.sabnzbd.get("api_key"):
            raise HTTPException(400, "sabnzbd api_key not configured")
        return Sabnzbd(config.sabnzbd["url"], config.sabnzbd["api_key"])

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.svg")
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "musiclib.svg", media_type="image/svg+xml")

    @app.get("/api/stats")
    def stats() -> Dict[str, Any]:
        return db.stats()

    @app.get("/api/tracks/{track_id}/playback")
    def playback_info(track_id: int) -> Dict[str, Any]:
        return playback.info(track_id)

    @app.get("/api/tracks/{track_id}/audio")
    async def track_audio(
        track_id: int, mode: Literal["original", "compatible"] = "original",
        start: float = Query(0, ge=0, allow_inf_nan=False),
    ) -> Response:
        track = playback.source(track_id)
        if mode == "original":
            if start:
                raise HTTPException(400, "Original playback uses byte ranges to seek.")
            return FileResponse(
                track.path, media_type=MEDIA_TYPES[audio_format(track.path)],
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        if track.duration and start >= track.duration:
            raise HTTPException(416, "Playback position is past the end of the track.")
        return await playback.compatible(track, start)

    @app.post("/api/scan")
    def scan(body: ScanBody) -> Dict[str, str]:
        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            return run_scan(body)
        return {"job_id": jobs.start("scan", run)}

    def run_scan(body: ScanBody) -> Dict[str, Any]:
        jobs.update("Discovering files")
        report = Scanner(config, db).scan(
            rescan=body.rescan,
            check_cancelled=jobs.check_cancelled,
            progress_state=lambda done, total, current: jobs.update(
                "Reading audio metadata", done, total, current
            ),
        )
        return {
            "scanned": report.scanned, "added": report.added,
            "updated": report.updated, "errors": report.errors,
            "removed": report.removed, "unchanged": report.skipped_unchanged,
        }

    @app.post("/api/doctor")
    @jobs.operation
    def doctor_run(body: ApplyBody) -> Dict[str, Any]:
        doc = Doctor(config, db)
        report = doc.check(
            deep=body.deep, apply_actions=body.apply, hard_delete=body.hard_delete,
            progress_state=jobs.update,
            outcome=jobs.outcome,
        )
        return {
            "checked": report.checked, "bad": report.bad, "suspect": report.suspect,
            "trashed": report.trashed, "deleted": report.deleted,
            "failed": report.failed, "skipped": report.skipped, "results": report.results, "failures": report.failures,
        }

    @app.get("/api/issues")
    def issues() -> List[Dict[str, Any]]:
        rows = db.issues_all()
        return [dict(row) for row in rows]

    @app.get("/api/issues/page")
    def issues_page(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
        severity: str = "", kind: str = "",
    ) -> Dict[str, Any]:
        where = "t.status != 'deleted'"
        params: List[Any] = []
        for column, value in (("i.severity", severity), ("i.kind", kind)):
            if value:
                where += f" AND {column}=?"
                params.append(value)
        join = f"FROM issues i JOIN tracks t ON t.id=i.track_id WHERE {where}"
        count = db.query_one("SELECT COUNT(*) AS n " + join, params)["n"]
        rows = db.query(
            "SELECT i.*, t.path " + join +
            " ORDER BY CASE i.severity WHEN 'bad' THEN 0 ELSE 1 END, t.path, i.id LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return {"items": [dict(row) for row in rows], "total": count, "offset": offset}

    @app.post("/api/issues/trash")
    def issues_trash(body: IssuesTrashBody) -> Dict[str, str]:
        if not body.track_ids or len(set(body.track_ids)) != len(body.track_ids):
            raise HTTPException(400, "Select distinct files to trash.")

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            result: Dict[str, Any] = {"trashed": 0, "deleted": 0, "failed": 0, "skipped": 0, "failures": []}
            total = len(body.track_ids)
            jobs.plan([{"track_id": track_id, "path": track.path if (track := db.get_track(track_id)) else "",
                        "reason": "Awaiting selected cleanup."} for track_id in body.track_ids])
            for offset in range(0, total, 200):
                jobs.check_cancelled()
                report = Doctor(config, db).trash_tracks(
                    body.track_ids[offset:offset + 200], hard_delete=body.hard_delete,
                    progress_state=lambda phase, done, count, current, base=offset: jobs.update(
                        phase, base + done, total, current,
                    ),
                    outcome=jobs.outcome,
                )
                for key in ("trashed", "deleted", "failed", "skipped"):
                    result[key] += getattr(report, key)
                result["failures"].extend(report.failures)
            return result

        return {"job_id": jobs.start("issues-trash", run)}

    def serialize_dupe_groups(groups) -> List[Dict[str, Any]]:
        return [
            {
                "group_id": group.group_id,
                "keep_id": group.keep.track.id if group.keep else None,
                "members": [
                    {
                        "id": member.track.id, "path": member.track.path,
                        "artist": member.track.artist, "album": member.track.album,
                        "title": member.track.title, "format": member.track.format,
                        "bitrate": member.track.bitrate, "bit_depth": member.track.bit_depth,
                        "quality_score": member.score,
                        "file_hash": member.track.file_hash,
                        "duration": member.track.duration,
                        "evidence": member.evidence,
                    }
                    for member in group.members
                ],
            }
            for group in groups
        ]

    @app.post("/api/dupes/find")
    @jobs.operation
    def dupes_find(body: ApplyBody) -> List[Dict[str, Any]]:
        fingerprint = body.fingerprint or (
            body.strategy or config.duplicates.get("strategy", "tags")
        ) == "fingerprint"
        if fingerprint:
            if not fpcalc_available():
                raise HTTPException(400, "fpcalc is not installed; audio fingerprints cannot be generated.")
            missing = db.tracks_missing_fingerprint()
            jobs.update("Generating audio fingerprints", 0, len(missing))
            computed = compute_all(
                config, db, missing,
                progress=lambda done, path: jobs.update(
                    "Generating audio fingerprints", done, len(missing), path
                ),
            )
            if computed != len(missing):
                for track in missing:
                    if not db.get_track(track.id).fingerprint:
                        jobs.outcome({
                            "track_id": track.id, "path": track.path, "status": "failed",
                            "reason": "Audio comparison unavailable for this file. Tags and exact hashes were still compared.",
                            "error": "Could not generate an audio fingerprint. Check the file in Bad Files before retrying.",
                        })
        jobs.update("Comparing duplicates")
        groups = DuplicateFinder(config, db).find(
            strategy=body.strategy, fingerprint_pass=fingerprint,
            progress_state=jobs.update,
        )
        return serialize_dupe_groups(groups)

    def scheduled_duplicates(job: Dict[str, Any]) -> Dict[str, Any]:
        run_scan(ScanBody())
        audio = bool(config.duplicates.get("schedule_fingerprint", True))
        return {"groups": dupes_find(ApplyBody(
            strategy="fingerprint" if audio else (
                "hash" if config.duplicates.get("strategy") == "hash" else "tags"
            ),
        ))}

    schedule = DuplicateSchedule(config, db, jobs, scheduled_duplicates)

    @app.on_event("startup")
    def start_schedule() -> None:
        schedule.start()

    @app.get("/api/dupes/schedule")
    def duplicate_schedule_status() -> Dict[str, Any]:
        return schedule.status()

    @app.get("/api/dupes")
    def dupes_list() -> List[Dict[str, Any]]:
        return serialize_dupe_groups(DuplicateFinder(config, db).persisted_groups())

    @app.get("/api/dupes/page")
    def dupes_page(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    ) -> Dict[str, Any]:
        groups = DuplicateFinder(config, db).persisted_groups()
        return {
            "items": serialize_dupe_groups(groups[offset:offset + limit]),
            "total": len(groups), "offset": offset,
        }

    @app.post("/api/dupes/apply")
    @jobs.operation
    def dupes_apply(body: KeepOverride) -> Dict[str, Any]:
        finder = DuplicateFinder(config, db)
        groups = finder.persisted_groups()
        if body.group_ids is not None:
            selected = set(body.group_ids)
            groups = [group for group in groups if group.group_id in selected]
            if {group.group_id for group in groups} != selected:
                raise HTTPException(409, "Duplicate groups changed. Run Find duplicates again.")
        for group in groups:
            expected = body.member_ids.get(group.group_id)
            if expected is not None and set(expected) != {m.track.id for m in group.members}:
                raise HTTPException(409, "Duplicate group membership changed. Review it again.")
        if body.snapshots is not None:
            members = [member.track for group in groups for member in group.members]
            if set(body.snapshots) != {track.id for track in members}:
                raise HTTPException(400, "Snapshots must cover exactly the reviewed duplicate groups.")
            if any(
                (body.snapshots[track.id].path, body.snapshots[track.id].file_hash)
                != (track.path, track.file_hash)
                for track in members
            ):
                raise HTTPException(409, "Duplicate files changed since review. Run Find duplicates again.")
        if not groups:
            return {"trashed": 0, "deleted": 0, "reclaimed_bytes": 0, "groups": 0}
        try:
            report = finder.apply(
                groups, hard_delete=body.hard_delete, keep_ids=body.keep_ids,
                progress_state=jobs.update,
                outcome=jobs.outcome,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "trashed": report.trashed, "deleted": report.deleted,
            "reclaimed_bytes": report.reclaimed_bytes, "groups": len(groups),
            "failed": len(report.failures), "failures": report.failures,
        }

    @app.post("/api/dupes/apply-job")
    def dupes_apply_job(body: KeepOverride) -> Dict[str, str]:
        return {"job_id": jobs.start("duplicates-apply", lambda job: dupes_apply(body))}

    @app.post("/api/dupes/manual")
    @jobs.operation
    def dupes_manual(body: ManualDupeBody) -> Dict[str, Any]:
        finder = DuplicateFinder(config, db)
        try:
            group = finder.manual_group(body.track_ids, body.keep_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        report = finder.apply([group], hard_delete=body.hard_delete, progress_state=jobs.update,
                              outcome=jobs.outcome)
        return {
            "trashed": report.trashed, "deleted": report.deleted,
            "reclaimed_bytes": report.reclaimed_bytes,
            "failed": len(report.failures), "failures": report.failures,
        }

    def selected_tracks(track_ids: List[int]) -> list:
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise HTTPException(400, "Select between 1 and 200 distinct tracks.")
        tracks = [db.get_track(track_id) for track_id in track_ids]
        if any(
            track is None or track.status == "deleted" or not is_inside(track.path, config.library)
            for track in tracks
        ):
            raise HTTPException(409, "Selected tracks changed or are unavailable. Refresh the library.")
        return tracks

    @app.post("/api/tracks/selected")
    def tracks_selected(body: TrackSelectionBody) -> List[Dict[str, Any]]:
        return [asdict(track) for track in selected_tracks(body.track_ids)]

    @app.post("/api/dupes/selected-preview")
    def selected_duplicates_preview(body: TrackSelectionBody) -> Dict[str, str]:
        selected_tracks(body.track_ids)

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            selected = selected_tracks(body.track_ids)
            tracks = [track for track in selected if track.status in ("ok", "suspect")]
            groups = DuplicateFinder(config, db).find(
                tracks=tracks, persist=False, progress_state=jobs.update, fingerprint_pass=True,
            )
            return {
                "groups": serialize_dupe_groups(groups),
                "tracks": [
                    {"id": track.id, "path": track.path, "file_hash": track.file_hash}
                    for track in tracks
                ],
                "skipped": len(selected) - len(tracks),
            }
        return {"job_id": jobs.start("album-duplicates-preview", run)}

    @app.post("/api/dupes/selected-apply")
    def selected_duplicates_apply(body: SelectedDuplicatesBody) -> Dict[str, str]:
        if not body.groups:
            raise HTTPException(400, "Select duplicate groups to clean up.")
        ids = [track_id for group in body.groups for track_id in group.track_ids]
        if len(set(ids)) != len(ids) or set(ids) - set(body.snapshots):
            raise HTTPException(400, "Groups must be distinct and covered by the reviewed snapshots.")
        selected_tracks(list(body.snapshots))
        if any(
            len(group.track_ids) < 2 or group.keep_id not in group.track_ids
            or group.hard_delete for group in body.groups
        ):
            raise HTTPException(400, "Choose one keeper per group. Only recoverable trash is supported here.")

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            tracks = selected_tracks(list(body.snapshots))
            for track in tracks:
                jobs.update("Checking reviewed duplicates", 0, len(tracks), track.path)
                snapshot = body.snapshots[track.id]
                if (
                    snapshot.path != track.path or snapshot.file_hash != track.file_hash
                    or Path(track.path).is_symlink() or file_hash(track.path) != snapshot.file_hash
                ):
                    raise ValueError("Files changed since duplicate review. Scan and review again.")
            finder = DuplicateFinder(config, db)
            groups = [finder.manual_group(group.track_ids, group.keep_id) for group in body.groups]
            report = finder.apply(groups, progress_state=jobs.update, outcome=jobs.outcome)
            return {
                "trashed": report.trashed, "failed": len(report.failures), "failures": report.failures,
                "remaining_ids": [
                    track.id for track in tracks if db.get_track(track.id).status != "deleted"
                ],
            }
        return {"job_id": jobs.start("album-duplicates-apply", run)}

    @app.post("/api/tracks/search")
    def tracks_search(body: TrackSearchBody) -> List[Dict[str, Any]]:
        return tracks_page(body)["items"]

    @app.get("/api/genres")
    def genres() -> List[str]:
        rows = db.query(
            "SELECT DISTINCT genre FROM tracks WHERE genre IS NOT NULL AND genre!='' "
            "ORDER BY genre COLLATE NOCASE"
        )
        return [row["genre"] for row in rows]

    @app.get("/api/formats")
    def formats() -> List[str]:
        rows = db.query(
            "SELECT DISTINCT format FROM tracks WHERE format IS NOT NULL AND format!='' "
            "ORDER BY format COLLATE NOCASE"
        )
        return [row["format"] for row in rows]

    @app.get("/api/metadata/fields")
    def metadata_fields() -> List[str]:
        return list(EDITABLE_FIELDS)

    @app.post("/api/metadata/lookup")
    @jobs.lookup_operation
    def metadata_lookup(body: MetadataLookupBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).propose(
                body.track_id, title=body.title, artist=body.artist, source=body.source,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/metadata/apply")
    @jobs.operation
    def metadata_apply(body: MetadataApplyBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).apply(
                body.review_id, body.candidate_index, body.fields
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.post("/api/metadata/identify")
    @jobs.lookup_operation
    def metadata_identify(body: MetadataIdentifyBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).identify(body.track_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/metadata/lookup-bulk")
    def metadata_lookup_bulk(body: MetadataBulkLookupBody) -> Dict[str, str]:
        if not 1 <= len(body.track_ids) <= 100 or len(set(body.track_ids)) != len(body.track_ids):
            raise HTTPException(400, "Select between 1 and 100 distinct tracks.")

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            reviewer = MetadataReview(config, db)
            reviews, errors = [], []
            total = len(body.track_ids)
            jobs.plan([{"track_id": track_id, "path": track.path if (track := db.get_track(track_id)) else "",
                        "reason": "Awaiting metadata search."} for track_id in body.track_ids])
            for index, track_id in enumerate(body.track_ids):
                jobs.update("Looking up catalog suggestions", index, total, str(track_id))
                try:
                    review = reviewer.propose(track_id, source=body.source)
                    reviews.append(review)
                    jobs.outcome({"track_id": track_id,
                                  "status": "completed" if review["candidates"] else "skipped",
                                  "reason": f"{len(review['candidates'])} suggestions found. No tags changed."})
                except (ValueError, RuntimeError, OSError) as exc:
                    errors.append({"track_id": track_id, "error": str(exc)})
                    jobs.outcome({"track_id": track_id, "status": "failed", "error": str(exc)})
                jobs.update("Looking up catalog suggestions", index + 1, total, str(track_id))
            return {"reviews": reviews, "errors": errors}

        return {"job_id": jobs.start("metadata-lookup", run, read_only=True)}

    @app.post("/api/metadata/apply-bulk")
    def metadata_apply_bulk(body: MetadataBulkApplyBody) -> Dict[str, str]:
        return start_reviewed_apply(body, limit=100)

    def start_reviewed_apply(body: MetadataBulkApplyBody, limit: int) -> Dict[str, str]:
        review_ids = [review.review_id for review in body.reviews]
        if not 1 <= len(review_ids) <= limit or len(set(review_ids)) != len(review_ids):
            raise HTTPException(400, f"Select between 1 and {limit} distinct reviews.")
        if any(
            not review.fields or set(review.fields) - set(EDITABLE_FIELDS)
            or review.candidate_index < 0
            for review in body.reviews
        ):
            raise HTTPException(400, "Choose a candidate and valid metadata fields for each review.")

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            reviewer = MetadataReview(config, db)
            applied, errors = [], []
            total = len(body.reviews)
            snapshots = {}
            for review in body.reviews:
                row = db.query_one("SELECT track_id,path FROM metadata_reviews WHERE id=?", (review.review_id,))
                if row:
                    snapshots[review.review_id] = dict(row)
            jobs.plan([{"review_id": review.review_id, **snapshots[review.review_id]}
                       for review in body.reviews if review.review_id in snapshots])
            for index, review in enumerate(body.reviews):
                jobs.update("Applying reviewed metadata", index, total, str(review.review_id))
                try:
                    result = reviewer.apply(
                        review.review_id, review.candidate_index, review.fields
                    )
                    applied.append({"review_id": review.review_id, **result})
                    jobs.outcome({**result, "status": "completed", "reason": "Saved reviewed metadata."})
                except (ValueError, RuntimeError, OSError, MutagenError) as exc:
                    errors.append({"review_id": review.review_id, "error": str(exc)})
                    if review.review_id in snapshots:
                        jobs.outcome({**snapshots[review.review_id], "status": "failed", "error": str(exc)})
                jobs.update("Applying reviewed metadata", index + 1, total, str(review.review_id))
            return {"applied": applied, "errors": errors}

        return {"job_id": jobs.start("metadata-apply", run)}

    @app.post("/api/metadata/album-edit/preview")
    @jobs.operation
    def album_edit_preview(body: AlbumEditBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).preview_album_edit(
                body.track_ids, body.fields, body.values,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/metadata/album-edit/apply")
    def album_edit_apply(body: MetadataBulkApplyBody) -> Dict[str, str]:
        if any(set(review.fields) - set(ALBUM_FIELDS) for review in body.reviews):
            raise HTTPException(400, "Only album fields can be applied by the album editor.")
        return start_reviewed_apply(body, limit=200)

    @app.post("/api/metadata/track-edit/preview")
    @jobs.operation
    def track_edit_preview(body: TrackEditBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).preview_track_edit(
                body.track_id, body.fields, body.values,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/metadata/track-edit/apply")
    def track_edit_apply(body: MetadataApplyBody) -> Dict[str, str]:
        if set(body.fields) - set(TRACK_FIELDS):
            raise HTTPException(400, "Only track fields can be applied by the track editor.")
        return start_reviewed_apply(MetadataBulkApplyBody(reviews=[body]), limit=1)

    @app.post("/api/metadata/numbering/preview")
    @jobs.operation
    def numbering_preview(body: NumberingPreviewBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).preview_numbering(
                body.track_ids, body.start_no, body.disc_no,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/metadata/numbering/apply")
    def numbering_apply(body: MetadataBulkApplyBody) -> Dict[str, str]:
        if any(set(review.fields) - {"track_no", "disc_no"} for review in body.reviews):
            raise HTTPException(400, "Only track/disc numbers can be applied by the numbering editor.")
        return start_reviewed_apply(body, limit=200)

    @app.post("/api/metadata/album-suggest")
    @jobs.lookup_operation
    def metadata_album_suggest(body: AlbumSuggestBody) -> Dict[str, Any]:
        try:
            return MetadataReview(config, db).suggest_album(
                body.track_ids, album=body.album, artist=body.artist, source=body.source,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/metadata/album-apply")
    def metadata_album_apply(body: AlbumApplyBody) -> Dict[str, str]:
        if not 1 <= len(body.track_ids) <= 200 or len(set(body.track_ids)) != len(body.track_ids):
            raise HTTPException(400, "Select between 1 and 200 distinct tracks.")
        if not body.fields or set(body.fields) - set(ALBUM_FIELDS):
            raise HTTPException(400, "Select one or more album fields to apply.")
        if all(body.values.get(field) in (None, "") for field in body.fields):
            raise HTTPException(400, "The selected album has no values for the chosen fields.")
        if body.snapshots is not None and set(body.snapshots) != set(body.track_ids):
            raise HTTPException(400, "Search snapshots must cover exactly the selected tracks.")
        snapshots = (
            {key: (value.path, value.file_hash) for key, value in body.snapshots.items()}
            if body.snapshots is not None else None
        )

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            jobs.update("Applying album tags", 0, len(body.track_ids))
            result = MetadataReview(config, db).apply_album(
                body.track_ids, body.fields, body.values, rename=body.rename, snapshots=snapshots,
                progress=jobs.update,
                outcome=jobs.outcome,
            )
            jobs.update("Applying album tags", len(body.track_ids), len(body.track_ids))
            return result

        return {"job_id": jobs.start("album-apply", run)}

    @app.post("/api/tracks/page")
    def tracks_page(body: TrackSearchBody) -> Dict[str, Any]:
        query = body.query.strip()
        limit = max(1, min(body.limit, 200))
        params: List[Any] = []
        where = "status IN ('ok', 'suspect', 'bad')" if body.status == "bad" else "status IN ('ok', 'suspect')"
        if query:
            pattern = f"%{query}%"
            where += " AND (path LIKE ? COLLATE NOCASE OR artist LIKE ? COLLATE NOCASE " \
                "OR album LIKE ? COLLATE NOCASE OR title LIKE ? COLLATE NOCASE)"
            params.extend([pattern] * 4)
        for column, value in (("genre", body.genre), ("format", body.format)):
            if value:
                where += f" AND {column}=?"
                params.append(value)
        if body.status in ("ok", "suspect", "bad"):
            where += " AND status=?"
            params.append(body.status)
        total = db.query_one(f"SELECT COUNT(*) AS n FROM tracks WHERE {where}", params)["n"]
        params.extend([limit, max(0, body.offset)])
        rows = db.query(
            "SELECT id, path, artist, album, title, genre, format, bitrate FROM tracks "
            f"WHERE {where} ORDER BY artist, album, title, path LIMIT ? OFFSET ?",
            params,
        )
        return {"items": [dict(row) for row in rows], "total": total, "offset": max(0, body.offset)}

    @app.get("/api/albums/page")
    def albums_page(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
        query: str = "", status: str = "", genre: str = "", format: str = "",
    ) -> Dict[str, Any]:
        page = db.album_groups_page(
            query=query.strip(), status=status, genre=genre, format=format,
            offset=offset, limit=limit,
        )
        items = []
        for row in page["items"]:
            mbid = row.get("mbid_release")
            cover_id = row.get("cover_track_id")
            cover_url = (
                f"/api/tracks/{cover_id}/cover?{urlencode({'v': row.get('cover_hash') or ''})}"
                if cover_id else CAA_FRONT_THUMB.format(mbid=mbid) if mbid else None
            )
            items.append({
                "album_artist": row["album_artist"], "album": row["album"],
                "year": row["year"], "genre": row["genre"],
                "track_count": row["track_count"], "bad_count": row["bad_count"],
                "has_art": bool(row["art_count"] or mbid),
                "mbid_release": mbid, "cover_track_id": cover_id, "cover_url": cover_url,
            })
        return {"items": items, "total": page["total"], "offset": page["offset"]}

    @app.get("/api/albums/detail")
    def albums_detail(
        album_artist: str = Query(...), album: str = Query(...), status: str = "",
    ) -> Dict[str, Any]:
        rows = db.album_tracks(album_artist, album, status=status)
        if not rows:
            raise HTTPException(404, "Album was not found in the library.")
        tracks = [dict(row) for row in rows]
        mbid = next((track["mbid_release"] for track in tracks if track.get("mbid_release")), None)
        cover_track = min(
            (track for track in tracks if track.get("has_embedded_art")),
            key=lambda track: track["path"], default=None,
        )
        cover_url = (
            f"/api/tracks/{cover_track['id']}/cover?{urlencode({'v': cover_track.get('file_hash') or ''})}"
            if cover_track else CAA_FRONT_THUMB.format(mbid=mbid) if mbid else None
        )
        first = tracks[0]
        return {
            "album_artist": album_artist, "album": album,
            "year": next((track["year"] for track in tracks if track.get("year")), None),
            "genre": next((track["genre"] for track in tracks if track.get("genre")), first.get("genre")),
            "track_count": len(tracks),
            "bad_count": sum(1 for track in tracks if track["status"] == "bad"),
            "mbid_release": mbid, "cover_url": cover_url, "tracks": tracks,
        }

    @app.get("/api/tracks/{track_id}/cover")
    def track_cover(track_id: int) -> Response:
        track = db.get_track(track_id)
        if track is None or track.status == "deleted":
            raise HTTPException(404, "Track is not available.")
        if not is_inside(track.path, config.library):
            raise HTTPException(400, "Track is outside the configured library.")
        cover = read_cover(track.path)
        if cover is None:
            raise HTTPException(404, "No cover art is available for this track.")
        data, mime = cover
        return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/trash")
    def trash_page(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
        include_restored: bool = False,
    ) -> Dict[str, Any]:
        items, total = db.deletions_page(offset, limit, include_restored)
        return {"items": items, "total": total, "offset": offset}

    @app.post("/api/trash/restore")
    @jobs.operation
    def trash_restore(body: RestoreBody) -> Dict[str, Any]:
        row = db.query_one(
            "SELECT * FROM deletions WHERE id=? AND restored_at IS NULL AND moved_to IS NOT NULL",
            (body.deletion_id,),
        )
        if row is None:
            raise HTTPException(404, "recoverable trash entry not found")
        source, target = str(Path(row["moved_to"]).resolve()), str(Path(row["path"]).resolve())
        if not is_inside(source, config.trash_dir) or not is_inside(target, config.library):
            raise HTTPException(400, "trash entry is outside the configured library")
        if not os.path.isfile(source):
            raise HTTPException(404, "trashed file is missing from disk")
        if os.path.lexists(target):
            raise HTTPException(409, "original path is occupied; move the existing file before restoring")
        try:
            move_no_replace(source, target)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise HTTPException(409, "original path became occupied during restore") from exc
            raise HTTPException(500, f"could not restore file: {exc}") from exc
        try:
            db.mark_restored(body.deletion_id)
            track = db.find_track_by_path(target)
            if track is not None:
                db.update_track_columns(track.id, status="suspect", path=target)
        except Exception as exc:
            raise HTTPException(
                500, f"file restored to {target}, but database history update failed: {exc}"
            ) from exc
        try:
            scan_report = Scanner(config, db).refresh_paths([target])
            scan_errors = [
                {
                    "path": path,
                    "error": "File was restored, but its catalog metadata could not be refreshed. Run Scan.",
                }
                for path in scan_report.error_paths
            ]
        except Exception as exc:
            logger.exception("Could not refresh catalog metadata after single-file restore")
            scan_errors = [{"path": target, "error": str(exc)}]
        return {"restored": target, "scan_errors": scan_errors}

    @app.post("/api/trash/restore-preview")
    def restore_preview(body: RestorePreviewBody) -> Dict[str, str]:
        if not 1 <= len(body.deletion_ids) <= 200 or len(set(body.deletion_ids)) != len(body.deletion_ids):
            raise HTTPException(400, "Select between 1 and 200 distinct trash entries.")
        return {"job_id": jobs.start(
            "restore-preview",
            lambda job: RestoreReview(config, db).preview(body.deletion_ids, progress=jobs.update),
        )}

    @app.get("/api/trash/restore-reviews/{review_id}")
    def restore_review_status(review_id: str) -> Dict[str, str]:
        try:
            return {"status": RestoreReview(config, db).status(review_id)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/trash/restore-reviewed")
    def restore_reviewed(body: ReviewedApplyBody) -> Dict[str, str]:
        preview_job = jobs.get(body.preview_job_id)
        if not preview_job or preview_job["kind"] != "restore-preview":
            raise HTTPException(400, "Choose a saved restore preview.")
        if preview_job["status"] != "done":
            raise HTTPException(400, "The restore preview has not completed successfully.")
        preview = jobs.result(body.preview_job_id)
        if not preview["ready"]:
            raise HTTPException(400, "This preview has no files ready to restore.")
        try:
            if RestoreReview(config, db).status(preview["review_id"]) != "review":
                raise ValueError("This restore preview was already used. Create a fresh preview.")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": jobs.start(
            "restore-reviewed",
            lambda job: RestoreReview(config, db).apply(preview["review_id"], progress=jobs.update, outcome=jobs.outcome),
        )}

    @app.post("/api/trash/preview-selected")
    def trash_preview_selected(body: TrackSelectionBody) -> Dict[str, str]:
        if not 1 <= len(body.track_ids) <= 200 or len(set(body.track_ids)) != len(body.track_ids):
            raise HTTPException(400, "Select between 1 and 200 distinct tracks.")
        return {"job_id": jobs.start(
            "trash-preview",
            lambda job: TrashReview(config, db).preview(body.track_ids, progress=jobs.update),
        )}

    @app.get("/api/trash/reviews/{review_id}")
    def trash_review_status(review_id: str) -> Dict[str, str]:
        try:
            return {"status": TrashReview(config, db).status(review_id)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/trash/apply-reviewed")
    def trash_apply_reviewed(body: ReviewedApplyBody) -> Dict[str, str]:
        preview_job = jobs.get(body.preview_job_id)
        if not preview_job or preview_job["kind"] != "trash-preview":
            raise HTTPException(400, "Choose a saved selected-track trash preview.")
        if preview_job["status"] != "done":
            raise HTTPException(400, "The trash preview has not completed successfully.")
        preview = jobs.result(body.preview_job_id)
        if not preview["ready"]:
            raise HTTPException(400, "This preview has no files ready to trash.")
        try:
            if TrashReview(config, db).status(preview["review_id"]) != "review":
                raise ValueError("This trash preview was already used. Create a fresh preview.")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": jobs.start(
            "trash-reviewed",
            lambda job: TrashReview(config, db).apply(preview["review_id"], progress=jobs.update, outcome=jobs.outcome),
        )}

    @app.post("/api/rename")
    @jobs.operation
    def rename_run(body: ApplyBody) -> Dict[str, Any]:
        jobs.update("Planning folder and filename changes")
        renamer = Renamer(config, db)
        report = renamer.plan(progress=jobs.update)
        if body.apply:
            jobs.update("Moving and renaming files")
            report = renamer.apply(report, progress=jobs.update, outcome=jobs.outcome)
        return {
            "applied": body.apply, "to_move": len(report.moves),
            "moved": report.moved, "already_ok": report.already_ok,
            "skipped": len(report.skipped), "errors": report.errors,
            "skipped_files": [{"path": path, "reason": reason} for path, reason in report.skipped],
            "moves": [
                {"source": m.source, "target": m.target, "status": m.status}
                for m in report.moves
            ],
        }

    @app.post("/api/rename/preview-selected")
    def rename_preview_selected(body: TrackSelectionBody) -> Dict[str, str]:
        if not 1 <= len(body.track_ids) <= 200 or len(set(body.track_ids)) != len(body.track_ids):
            raise HTTPException(400, "Select between 1 and 200 distinct tracks.")
        return {"job_id": jobs.start(
            "rename-preview",
            lambda job: Renamer(config, db).preview_selected(body.track_ids, progress=jobs.update),
        )}

    @app.post("/api/rename/apply-reviewed")
    def rename_apply_reviewed(body: ReviewedApplyBody) -> Dict[str, str]:
        preview_job = jobs.get(body.preview_job_id)
        if not preview_job or preview_job["kind"] != "rename-preview":
            raise HTTPException(400, "Choose a saved selected-track rename preview.")
        if preview_job["status"] != "done":
            raise HTTPException(400, "The rename preview has not completed successfully.")
        preview = jobs.result(body.preview_job_id)
        if not preview["ready"]:
            raise HTTPException(400, "This preview has no files ready to rename.")
        return {"job_id": jobs.start(
            "rename-reviewed",
            lambda job: Renamer(config, db).apply_selected(preview, progress=jobs.update, outcome=jobs.outcome),
        )}

    @app.get("/api/albums")
    def albums(
        missing_art: bool = False, missing_tracks: bool = False
    ) -> List[Dict[str, Any]]:
        if missing_art:
            rows = db.albums_all("art_status IN ('missing','none','unknown','small')")
        elif missing_tracks:
            rows = db.albums_all(
                "missing_tracks IS NOT NULL AND missing_tracks != '[]'"
            )
        else:
            rows = db.albums_all()
        result = []
        for album in rows:
            result.append(
                {
                    "album_artist": album.album_artist, "album": album.album,
                    "year": album.year, "art_status": album.art_status,
                    "have_tracks": album.have_tracks,
                    "expected_tracks": album.expected_tracks,
                    "missing_tracks": album.missing_tracks,
                }
            )
        return result

    @app.post("/api/artwork/preview")
    def artwork_preview(body: ArtworkPreviewBody) -> Dict[str, str]:
        if not 1 <= len(body.track_ids) <= 200 or len(set(body.track_ids)) != len(body.track_ids):
            raise HTTPException(400, "Select between 1 and 200 distinct tracks.")
        return {"job_id": jobs.start(
            "artwork-preview",
            lambda job: ArtworkReview(config, db).preview(
                body.track_ids, body.image_base64, body.filename, progress=jobs.update,
            ),
        )}

    @app.get("/api/artwork/reviews/{review_id}/cover")
    def reviewed_cover(review_id: str) -> Response:
        try:
            data, mime = ArtworkReview(config, db).cover(review_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(
            content=data, media_type=mime,
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/artwork/reviews/{review_id}")
    def artwork_review_status(review_id: str) -> Dict[str, str]:
        try:
            return {"status": ArtworkReview(config, db).status(review_id)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/artwork/apply-reviewed")
    def artwork_apply_reviewed(body: ReviewedApplyBody) -> Dict[str, str]:
        preview_job = jobs.get(body.preview_job_id)
        if not preview_job or preview_job["kind"] != "artwork-preview":
            raise HTTPException(400, "Choose a saved artwork preview.")
        if preview_job["status"] != "done":
            raise HTTPException(400, "The artwork preview has not completed successfully.")
        preview = jobs.result(body.preview_job_id)
        if not preview["ready"]:
            raise HTTPException(400, "This preview has no files ready for artwork.")
        if ArtworkReview(config, db).status(preview["review_id"]) != "review":
            raise HTTPException(400, "This artwork preview was already used. Create a fresh preview.")
        return {"job_id": jobs.start(
            "artwork-reviewed",
            lambda job: ArtworkReview(config, db).apply(preview["review_id"], progress=jobs.update, outcome=jobs.outcome),
        )}

    @app.post("/api/art")
    @jobs.operation
    def art_run(body: ApplyBody) -> Dict[str, Any]:
        artwork = Artwork(config, db)
        brainz = Brainz(config, db)
        albums = artwork.albums_needing_art(body.replace_small)
        if not body.apply:
            return {
                "applied": False,
                "albums": [
                    {"artist": a.album_artist, "album": a.album, "status": a.art_status}
                    for a in albums
                ],
            }
        results = []
        jobs.update("Downloading artwork", 0, len(albums))
        for index, album in enumerate(albums, start=1):
            jobs.check_cancelled()
            result = artwork.process_album(
                album, brainz.resolve_release, body.replace_small
            )
            results.append(
                {
                    "artist": result.artist, "album": result.album,
                    "status": result.status, "detail": result.detail,
                    "embedded": result.embedded,
                }
            )
            jobs.update("Downloading artwork", index, len(albums), album.album)
        return {"applied": True, "albums": results}

    @app.post("/api/brainz/refresh")
    def brainz_refresh() -> Dict[str, str]:
        brainz = Brainz(config, db)

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            jobs.update("Comparing albums with MusicBrainz")
            gaps = brainz.refresh_albums(
                progress=lambda done, count, current: jobs.update(
                    "Comparing albums with MusicBrainz", done, count, current
                )
            )
            return {"albums_with_gaps": len([g for g in gaps if g.missing])}

        return {"job_id": jobs.start("brainz", run)}

    @app.get("/api/missing")
    def missing() -> List[Dict[str, Any]]:
        brainz = Brainz(config, db)
        return [
            {
                "artist": gap.album.album_artist, "album": gap.album.album,
                "missing": [t.title for t in gap.missing],
            }
            for gap in brainz.albums_with_gaps()
        ]

    @app.post("/api/search")
    def search(body: SearchBody) -> List[Dict[str, Any]]:
        clients = config.indexer_clients()
        if not clients:
            raise HTTPException(400, "no indexers configured")
        results = search_all(clients, body.query, body.limit)
        return [
            {
                "indexer": r.indexer, "title": r.title, "url": r.url,
                "size": r.size, "score": r.score, "posted": r.posted,
            }
            for r in results
        ]

    @app.post("/api/sab/add")
    def sab_add(body: SabAddBody) -> Dict[str, str]:
        try:
            nzo_id = sab_client().add_url(body.url, name=body.name)
        except SabnzbdError as exc:
            raise HTTPException(502, str(exc)) from exc
        db.record_download(nzo_id, body.name or body.url, body.url, body.indexer)
        return {"nzo_id": nzo_id}

    @app.get("/api/sab/queue")
    def sab_queue() -> List[Dict[str, Any]]:
        try:
            return sab_client().queue()
        except SabnzbdError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/sab/history")
    def sab_history() -> List[Dict[str, Any]]:
        try:
            return sab_client().history()
        except SabnzbdError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/settings")
    def settings_get() -> Dict[str, Any]:
        result: Dict[str, Any] = {"library": config.library}
        for section in EDITABLE_SECTIONS:
            values = dict(getattr(config, section))
            for key in SECRET_KEYS:
                if key in values:
                    values[key + "_set"] = bool(values[key])
                    values[key] = ""
            result[section] = values
        result["indexers"] = [
            {**{k: v for k, v in i.items() if k != "api_key"},
             "api_key": "", "api_key_set": bool(i.get("api_key"))}
            for i in config.indexers
        ]
        return result

    @app.put("/api/settings")
    @jobs.operation
    def settings_put(body: SettingsBody) -> Dict[str, Any]:
        updates: Dict[str, Dict[str, Any]] = {}
        for section in EDITABLE_SECTIONS:
            incoming = getattr(body, section)
            if incoming is None:
                continue
            current = getattr(config, section)
            merged = dict(current)
            for key, value in incoming.items():
                if key.endswith("_set") or key not in current:
                    continue
                if key in SECRET_KEYS and value in ("", None):
                    continue
                merged[key] = _coerce(value, current[key])
            updates[section] = merged
        if "acoustid" in updates:
            if body.acoustid and body.acoustid.get("clear_api_key") is True:
                updates["acoustid"]["api_key"] = ""
            key = updates["acoustid"]["api_key"]
            if len(key) > 200 or any(not 33 <= ord(char) <= 126 for char in key):
                raise HTTPException(400, "The AcoustID application key must contain at most 200 non-space ASCII characters.")
        if "catalog" in updates and updates["catalog"].get("album_source", "all") not in ALBUM_SOURCES:
            raise HTTPException(400, "Choose a valid default album lookup source.")
        if "duplicates" in updates:
            duplicate_settings = updates["duplicates"]
            if duplicate_settings.get("schedule_hours", 0) not in (0, 24, 168):
                raise HTTPException(400, "Choose disabled, daily (24 hours), or weekly (168 hours) discovery.")
            if duplicate_settings.get("strategy") not in ("hash", "tags", "fingerprint"):
                raise HTTPException(400, "Choose hash, tags, or fingerprint duplicate detection.")
            try:
                tolerance = float(duplicate_settings.get("duration_tolerance", 5))
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "Duplicate duration tolerance must be a number.") from exc
            if not 0 <= tolerance <= 30:
                raise HTTPException(400, "Duplicate duration tolerance must be between 0 and 30 seconds.")
            duplicate_settings["duration_tolerance"] = tolerance
            if duplicate_settings.get("schedule_hours") and duplicate_settings.get("schedule_fingerprint") and not fpcalc_available():
                raise HTTPException(400, "Install fpcalc before enabling scheduled audio comparison.")
        new_indexers = None
        if body.indexers is not None:
            new_indexers = []
            for pos, item in enumerate(body.indexers):
                url = str(item.get("url", "")).strip()
                name = str(item.get("name", "")).strip()
                if not url and not name:
                    continue
                key = str(item.get("api_key") or "").strip()
                if not key:
                    old = str(item.get("old_name", name))
                    prev = next((i for i in config.indexers if i.get("name") == old), None)
                    if prev is None and pos < len(config.indexers):
                        prev = config.indexers[pos]
                    key = (prev or {}).get("api_key", "")
                new_indexers.append(
                    {"name": name, "url": url, "api_key": key,
                     "categories": str(item.get("categories", "3000")).strip() or "3000"}
                )
        previous_sections = {section: dict(getattr(config, section)) for section in updates}
        previous_indexers = list(config.indexers)
        for section, merged in updates.items():
            getattr(config, section).update(merged)
        if new_indexers is not None:
            config.indexers[:] = new_indexers
        try:
            path = config.save()
        except (OSError, ValueError) as exc:
            for section, previous in previous_sections.items():
                getattr(config, section).clear()
                getattr(config, section).update(previous)
            config.indexers[:] = previous_indexers
            raise HTTPException(500, f"could not write config: {exc}") from exc
        schedule.status()
        return {"saved": True, "path": str(path)}

    @app.post("/api/tasks/{kind}")
    def task_start(kind: str, body: ApplyBody) -> Dict[str, str]:
        actions = {
            "doctor": lambda: doctor_run(body),
            "duplicates": lambda: {"groups": dupes_find(body)},
            "rename": lambda: rename_run(body),
            "art": lambda: art_run(body),
        }
        if kind not in actions:
            raise HTTPException(404, "unknown task")
        return {"job_id": jobs.start(kind, lambda job: actions[kind]())}

    @app.get("/api/jobs")
    def job_list() -> List[Dict[str, Any]]:
        return jobs.list()

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> Dict[str, Any]:
        return jobs.cancel(job_id)

    @app.get("/api/jobs/{job_id}/results/{section}")
    def job_results(
        job_id: str, section: str, offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
    ) -> Dict[str, Any]:
        result = jobs.result(job_id)
        items = result.get(section)
        if not isinstance(items, list):
            raise HTTPException(404, "result section not found")
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset}

    @app.get("/api/jobs/{job_id}/items")
    def job_items(
        job_id: str, status: Literal["", "completed", "failed", "skipped", "stopped", "pending"] = "",
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    ) -> Dict[str, Any]:
        if jobs.get(job_id) is None:
            raise HTTPException(404, "job not found")
        return {
            "items": jobs.items(job_id, status, offset, limit),
            "total": jobs.item_count(job_id, status), "offset": offset,
        }

    @app.get("/api/jobs/{job_id}/retry-selection")
    def retry_selection(job_id: str) -> Dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job["status"] not in ("done", "cancelled", "error"):
            raise HTTPException(409, "Stop the job or wait for completion before preparing a new review.")
        items = [item for item in jobs.items(job_id) if item["status"] in ("failed", "stopped")]
        ids = list(dict.fromkeys(
            track_id for item in items for track_id in (item.get("track_id"), item.get("keep_id"))
            if track_id is not None
        ))
        if not ids:
            raise HTTPException(400, "No retryable library tracks. Review file details and refresh the relevant workspace.")
        if len(ids) > 200:
            raise HTTPException(400, "More than 200 tracks need attention. Select a smaller batch from Library using the file details.")
        tracks = [track for track_id in ids if (track := db.get_track(track_id)) and track.status != "deleted"]
        if not tracks:
            raise HTTPException(409, "These files are no longer in Library. Refresh the catalog and check Recovery.")
        return {
            "tracks": [asdict(track) for track in tracks], "kind": job["kind"],
            "unavailable": len(ids) - len(tracks),
        }

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job

    @app.on_event("shutdown")
    def shutdown() -> None:
        schedule.close()
        jobs.close()
        db.close()

    return app
