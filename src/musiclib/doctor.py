from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import Config
from .db import Database
from .fsops import delete_file, trash_file
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
    results: List[Dict] = field(default_factory=list)


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
    ) -> DoctorReport:
        if deep is None:
            deep = bool(self.config.doctor.get("deep_decode", True))
        min_bitrate = int(self.config.doctor.get("min_bitrate", 128))
        min_duration = float(self.config.doctor.get("min_duration", 10))
        report = DoctorReport()
        tracks = [t for t in self.db.all_tracks() if t.status in ("ok", "suspect", "bad")]
        report.checked = len(tracks)
        deep_targets: List[Track] = []

        for track in tracks:
            issues: List[Issue] = [
                i for i in track.issues
                if i.kind not in ("decode_error", "short_file", "low_bitrate")
            ]
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
            if issues:
                track.issues = issues
                track.status = "bad" if structural_bad else "suspect"
                self.db.upsert_track(track)
                severity = "bad" if structural_bad else "warn"
                report.results.append(
                    {
                        "path": track.path,
                        "severity": severity,
                        "issues": [i.kind for i in issues],
                    }
                )
                if structural_bad:
                    report.bad += 1
                else:
                    report.suspect += 1
            if track.status != "bad":
                deep_targets.append(track)

        if deep and self.ffmpeg and deep_targets:
            done = 0
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
                        report.bad += 1
                        report.results.append(
                            {"path": track.path, "severity": "bad", "issues": ["decode_error"]}
                        )
                    elif track.status == "suspect":
                        self.db.upsert_track(track)
                    else:
                        track.issues = []
                        track.status = "ok"
                        self.db.upsert_track(track)
        elif deep and not self.ffmpeg:
            report.results.append(
                {
                    "path": "",
                    "severity": "warn",
                    "issues": ["ffmpeg not found; deep decode checks skipped (brew install ffmpeg)"],
                }
            )

        if apply_actions:
            for track in self.db.all_tracks(status="bad"):
                try:
                    if hard_delete:
                        delete_file(track.path)
                        self.db.record_deletion(
                            track.path, track.size, track.file_hash, "doctor:bad", None
                        )
                        report.deleted += 1
                    else:
                        dest = trash_file(track.path, self.config.trash_dir)
                        self.db.record_deletion(
                            track.path, track.size, track.file_hash, "doctor:bad", dest
                        )
                        report.trashed += 1
                    self.db.update_track_columns(track.id, status="deleted")
                except FileNotFoundError:
                    self.db.update_track_columns(track.id, status="deleted")
        return report
