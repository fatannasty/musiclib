from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..artwork import Artwork
from ..brainz import Brainz
from ..config import Config
from ..db import Database
from ..doctor import Doctor
from ..duplicates import DuplicateFinder
from ..rename import Renamer
from ..scanner import Scanner
from ..usenet import Sabnzbd, SabnzbdError, search_all

STATIC_DIR = Path(__file__).parent / "static"


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn) -> str:
        job_id = uuid.uuid4().hex[:12]

        def runner() -> None:
            job = self._jobs[job_id]
            job["status"] = "running"
            try:
                result = fn(job)
                job["result"] = result
                job["status"] = "done"
            except Exception as exc:
                job["status"] = "error"
                job["result"] = {"error": str(exc)}
            job["finished"] = time.time()

        with self._lock:
            self._jobs[job_id] = {
                "id": job_id, "kind": kind, "status": "queued",
                "started": time.time(), "finished": None, "result": None, "log": [],
            }
        threading.Thread(target=runner, daemon=True).start()
        return job_id

    def log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["log"].append(line)
                if len(job["log"]) > 500:
                    del job["log"][:-500]

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None


class ApplyBody(BaseModel):
    apply: bool = False
    hard_delete: bool = False
    deep: Optional[bool] = None
    replace_small: bool = False
    strategy: Optional[str] = None
    fingerprint: bool = False


class KeepOverride(BaseModel):
    hard_delete: bool = False
    keep_ids: Dict[int, int] = {}


class SearchBody(BaseModel):
    query: str
    limit: int = 25


class SabAddBody(BaseModel):
    url: str
    name: Optional[str] = None
    indexer: str = ""


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="musiclib", docs_url=None, redoc_url=None)
    db = Database(config.db_path)
    jobs = JobRegistry()

    def sab_client() -> Sabnzbd:
        if not config.sabnzbd.get("api_key"):
            raise HTTPException(400, "sabnzbd api_key not configured")
        return Sabnzbd(config.sabnzbd["url"], config.sabnzbd["api_key"])

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/stats")
    def stats() -> Dict[str, Any]:
        return db.stats()

    @app.post("/api/scan")
    def scan() -> Dict[str, str]:
        scanner = Scanner(config, db)

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            report = scanner.scan(
                progress=lambda done, current: jobs.log(
                    job["id"], f"{done} {Path(current).name}"
                )
            )
            return {
                "scanned": report.scanned, "added": report.added,
                "updated": report.updated, "errors": report.errors,
                "removed": report.removed,
            }

        return {"job_id": jobs.start("scan", run)}

    @app.post("/api/doctor")
    def doctor_run(body: ApplyBody) -> Dict[str, Any]:
        doc = Doctor(config, db)
        report = doc.check(
            deep=body.deep, apply_actions=body.apply, hard_delete=body.hard_delete
        )
        return {
            "checked": report.checked, "bad": report.bad, "suspect": report.suspect,
            "trashed": report.trashed, "deleted": report.deleted,
            "results": report.results[:500],
        }

    @app.get("/api/issues")
    def issues() -> List[Dict[str, Any]]:
        rows = db.issues_all()
        return [dict(row) for row in rows[:500]]

    @app.get("/api/dupes")
    def dupes_list() -> List[Dict[str, Any]]:
        rows = db.query(
            "SELECT g.id AS group_id, g.keep_id, t.* FROM dupe_groups g "
            "JOIN dupe_members m ON m.group_id = g.id "
            "JOIN tracks t ON t.id = m.track_id WHERE g.resolved = 0 "
            "ORDER BY g.id, t.quality_score DESC"
        )
        groups: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            gid = row["group_id"]
            group = groups.setdefault(gid, {"group_id": gid, "keep_id": row["keep_id"], "members": []})
            group["members"].append(dict(row))
        return list(groups.values())

    @app.post("/api/dupes/apply")
    def dupes_apply(body: KeepOverride) -> Dict[str, Any]:
        finder = DuplicateFinder(config, db)
        groups = finder.find()
        if not groups:
            return {"trashed": 0, "deleted": 0, "reclaimed_bytes": 0, "groups": 0}
        report = finder.apply(groups, hard_delete=body.hard_delete, keep_ids=body.keep_ids)
        return {
            "trashed": report.trashed, "deleted": report.deleted,
            "reclaimed_bytes": report.reclaimed_bytes, "groups": len(groups),
        }

    @app.post("/api/rename")
    def rename_run(body: ApplyBody) -> Dict[str, Any]:
        renamer = Renamer(config, db)
        report = renamer.plan()
        if body.apply:
            report = renamer.apply(report)
        return {
            "applied": body.apply, "to_move": len(report.moves),
            "moved": report.moved, "already_ok": report.already_ok,
            "skipped": len(report.skipped), "errors": report.errors[:100],
            "moves": [
                {"source": m.source, "target": m.target, "status": m.status}
                for m in report.moves[:200]
            ],
        }

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

    @app.post("/api/art")
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
        for album in albums:
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
        return {"applied": True, "albums": results}

    @app.post("/api/brainz/refresh")
    def brainz_refresh() -> Dict[str, str]:
        brainz = Brainz(config, db)

        def run(job: Dict[str, Any]) -> Dict[str, Any]:
            gaps = brainz.refresh_albums()
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

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job

    @app.on_event("shutdown")
    def shutdown() -> None:
        db.close()

    return app
