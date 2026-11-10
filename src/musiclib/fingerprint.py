from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from .config import Config
from .db import Database
from .models import Track


def fpcalc_available() -> Optional[str]:
    return shutil.which("fpcalc")


def _popcount(value: int) -> int:
    return bin(value).count("1")


def compute_fingerprint(path: str, length: int = 90) -> Dict:
    if not fpcalc_available():
        return {"path": path, "fingerprint": None, "duration": None}
    cmd = ["fpcalc", "-raw", "-length", str(length), path]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"path": path, "fingerprint": None, "duration": None}
    if proc.returncode != 0:
        return {"path": path, "fingerprint": None, "duration": None}
    duration = None
    words: List[int] = []
    for line in proc.stdout.splitlines():
        if line.startswith("DURATION="):
            try:
                duration = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("FINGERPRINT="):
            raw = line.split("=", 1)[1].strip()
            try:
                words = [int(w) for w in raw.split(",") if w]
            except ValueError:
                words = []
    if not words:
        return {"path": path, "fingerprint": None, "duration": duration}
    return {"path": path, "fingerprint": ",".join(str(w) for w in words), "duration": duration}


def similarity(fp_a: str, fp_b: str) -> float:
    words_a = [int(w) for w in fp_a.split(",")]
    words_b = [int(w) for w in fp_b.split(",")]
    if not words_a or not words_b:
        return 0.0
    length = min(len(words_a), len(words_b))
    if length < 8:
        return 0.0
    total_bits = 0
    for i in range(length):
        total_bits += _popcount(words_a[i] ^ words_b[i])
    return 1.0 - (total_bits / (length * 32))


def fingerprint_missing(config: Config, db: Database) -> List[Track]:
    return db.tracks_missing_fingerprint()


def compute_all(
    config: Config,
    db: Database,
    tracks: List[Track],
    workers: Optional[int] = None,
    progress: Optional[Callable[[int, str], None]] = None,
) -> int:
    import os

    if not fpcalc_available():
        return 0
    workers = workers or max(4, (os.cpu_count() or 4) - 1)
    computed = 0
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(compute_fingerprint, t.path): t for t in tracks}
        try:
            for future in as_completed(futures):
                track = futures[future]
                result = future.result()
                done += 1
                if result["fingerprint"]:
                    db.update_track_columns(
                        track.id, fingerprint=result["fingerprint"],
                        duration=result["duration"] or track.duration,
                    )
                    computed += 1
                if progress:
                    progress(done, track.path)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return computed
