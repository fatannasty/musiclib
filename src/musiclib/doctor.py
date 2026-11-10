from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import Config
from .db import Database
from .fsops import delete_file, is_inside, trash_file
from .models import LOSSLESS_FORMATS, Issue, Track


def find_binary(name: str) -> Optional[str]:
    return shutil.which(name)


def decode_check_ffmpeg(path: str) -> Dict:
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        return {"path": path, "ok": False, "error": "decode timed out after 120s"}
    except OSError as exc:
        return {"path": path, "ok": False, "error": str(exc)}
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"path": path, "ok": False, "error": stderr or f"exit code {proc.returncode}"}
    if stderr:
        return {"path": path, "ok": False, "error": stderr[:2000]}
    return {"path": path, "ok": True, "error": None}


@dataclass
class DoctorReport:
    checked: int = 0
    bad: int = 0
    suspect: int = 0
    trashed: int = 0
    deleted: int = 0
    failed: int = 0
    results: List[Dict] = field(default_factory=list)
    failures: List[Dict] = field(default_factory=list)


class Doctor:
    def __init__(self, config: Config, db: Database, workers: Optional[int] = None):
        self.config = config
        self.db = db
        self.workers = workers or max(4, (os.cpu_count() or 4) - 1)
        self.ffmpeg = find_binary("ffmpeg")

    def check(
        self,
        deep: Optional[bool] = None,
        apply_actions: bool = False,
        hard_delete: bool = False,
        progress: Optional[Callable[[int, str], None]] = None,
        progress_state: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> DoctorReport:
        if deep is None:
            deep = bool(self.config.doctor.get("deep_decode", True))
        min_bitrate = int(self.config.doctor.get("min_bitrate", 128))
        min_duration = float(self.config.doctor.get("min_duration", 10))
        report = DoctorReport()
        tracks = [t for t in self.db.all_tracks() if t.status in ("ok", "suspect", "bad")]
        report.checked = len(tracks)
        deep_targets: List[Track] = []

        if progress_state:
            progress_state("Checking metadata", 0, len(tracks), "")
        for index, track in enumerate(tracks, start=1):
            issues: List[Issue] = [
                i for i in track.issues
                if i.kind not in ("short_file", "low_bitrate", "missing_file")
                and not (i.kind == "decode_error" and deep and self.ffmpeg)
            ]
            if not os.path.isfile(track.path):
                issues.append(Issue("missing_file", "File is missing on disk", "bad"))
            if track.duration is not None and track.duration < min_duration:
                issues.append(
                    Issue(
                        "short_file",
                        f"duration {track.duration:.1f}s below threshold {min_duration}s",
                        "warn",
                    )
                )
            if (
                track.bitrate is not None
                and track.format not in LOSSLESS_FORMATS
                and track.bitrate < min_bitrate
            ):
                issues.append(
                    Issue(
                        "low_bitrate",
                        f"{track.bitrate}kbps below threshold {min_bitrate}kbps",
                        "warn",
                    )
                )
            structural_bad = [i for i in issues if i.severity == "bad"]
            track.issues = issues
            track.status = "bad" if structural_bad else ("suspect" if issues else "ok")
            self.db.upsert_track(track)
            if track.status != "bad":
                deep_targets.append(track)
            if progress_state:
                progress_state("Checking metadata", index, len(tracks), track.path)

        if deep and self.ffmpeg and deep_targets:
            done = 0
            if progress_state:
                progress_state("Decoding audio", 0, len(deep_targets), "")
            with ProcessPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(decode_check_ffmpeg, t.path): t for t in deep_targets}
                for future in as_completed(futures):
                    track = futures[future]
                    result = future.result()
                    done += 1
                    if progress:
                        progress(done, track.path)
                    if not result["ok"]:
                        track.issues.append(Issue("decode_error", result["error"], "bad"))
                        track.status = "bad"
                        self.db.upsert_track(track)
                    elif track.status == "suspect":
                        self.db.upsert_track(track)
                    else:
                        track.issues = []
                        track.status = "ok"
                        self.db.upsert_track(track)
                    if progress_state:
                        progress_state("Decoding audio", done, len(deep_targets), track.path)
        elif deep and not self.ffmpeg:
            report.results.append(
                {
                    "path": "",
                    "severity": "warn",
                    "issues": ["ffmpeg not found; deep decode checks skipped (brew install ffmpeg)"],
                }
            )

        for track in tracks:
            if track.status == "bad":
                report.bad += 1
            elif track.status == "suspect":
                report.suspect += 1
            if track.issues:
                report.results.append({
                    "path": track.path,
                    "severity": "bad" if track.status == "bad" else "warn",
                    "issues": [issue.kind for issue in track.issues],
                    "details": [issue.detail for issue in track.issues],
                })

        if apply_actions:
            bad_tracks = self.db.all_tracks(status="bad")
            if progress_state:
                progress_state("Moving bad files to trash", 0, len(bad_tracks), "")
            for index, track in enumerate(bad_tracks, start=1):
                self._trash_one(track, report, hard_delete=hard_delete, reason="doctor:bad")
                if progress_state:
                    progress_state("Moving bad files to trash", index, len(bad_tracks), track.path)
        return report

    def trash_tracks(
        self,
        track_ids: List[int],
        hard_delete: bool = False,
        progress_state: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> DoctorReport:
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise ValueError("Select between 1 and 200 distinct files.")
        report = DoctorReport()
        for index, track_id in enumerate(track_ids, start=1):
            track = self.db.get_track(track_id)
            if track is None or track.status == "deleted":
                report.failed += 1
                report.failures.append({"track_id": track_id, "error": "Track is not available."})
            else:
                self._trash_one(track, report, hard_delete=hard_delete, reason="doctor:selected")
            if progress_state:
                progress_state("Moving selected files to trash", index, len(track_ids), str(track_id))
        return report

    def _trash_one(
        self, track: Track, report: DoctorReport, hard_delete: bool, reason: str
    ) -> None:
        if not is_inside(track.path, self.config.library):
            report.failed += 1
            report.failures.append({"track_id": track.id, "path": track.path, "error": "File is outside the library."})
            return
        try:
            if hard_delete:
                delete_file(track.path)
                self.db.record_deletion(track.path, track.size, track.file_hash, reason, None)
                report.deleted += 1
            else:
                dest = trash_file(track.path, self.config.trash_dir)
                self.db.record_deletion(track.path, track.size, track.file_hash, reason, dest)
                report.trashed += 1
            self.db.update_track_columns(track.id, status="deleted")
        except FileNotFoundError:
            self.db.update_track_columns(track.id, status="deleted")
            report.trashed += 1
        except OSError as exc:
            report.failed += 1
            report.failures.append({
                "track_id": track.id, "path": track.path,
                "error": f"Could not move this file to trash: {exc}",
            })
