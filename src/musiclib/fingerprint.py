from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
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


@lru_cache(maxsize=2048)
def _words(fingerprint: str) -> tuple:
    return tuple(int(word) & 0xFFFFFFFF for word in fingerprint.split(",") if word)


def similarity(fp_a: str, fp_b: str, minimum: float = 0.0) -> float:
    words_a, words_b = _words(fp_a), _words(fp_b)
    if not words_a or not words_b:
        return 0.0
    if len(set(words_a)) < 4 or len(set(words_b)) < 4:
        return 0.0
    length = min(len(words_a), len(words_b))
    if length < 8:
        return 0.0
    best = 0.0
    offsets = [0] + [offset for shift in range(1, min(16, length // 5) + 1)
                     for offset in (shift, -shift)]
    for offset in offsets:
        start_a, start_b = max(0, offset), max(0, -offset)
        overlap = min(len(words_a) - start_a, len(words_b) - start_b)
        if overlap < max(8, int(length * .8)):
            continue
        total_bits = 0
        allowed_bits = (1 - max(best, minimum)) * overlap * 32 + 1e-9
        for a, b in zip(words_a[start_a:start_a + overlap], words_b[start_b:start_b + overlap]):
            total_bits += _popcount(a ^ b)
            if total_bits > allowed_bits:
                break
        if total_bits > allowed_bits:
            continue
        best = max(best, 1.0 - total_bits / (overlap * 32))
        if best == 1.0 or (minimum and best >= minimum):
            break
    return best


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
                        duration=track.duration if track.duration is not None else result["duration"],
                    )
                    computed += 1
                if progress:
                    progress(done, track.path)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return computed
