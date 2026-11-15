from __future__ import annotations

import re
from typing import Callable, List, Optional, Set

from .duplicates import duplicate_text
from .models import Track


def _genres(value: Optional[str]) -> Set[str]:
    return {
        normalized for part in re.split(r"[,;/]", value or "")
        if (normalized := duplicate_text(part))
    }


def radio_tracks(
    seed: Track, tracks: List[Track], limit: int = 50,
    available: Optional[Callable[[Track], bool]] = None,
) -> List[Track]:
    """Rank library tracks by shared genre and artist, without claiming audio similarity."""
    seed_artist = duplicate_text(seed.artist)
    if seed_artist in {"unknown", "unknown artist", "various artists", "va"}:
        seed_artist = ""
    seed_genres = _genres(seed.genre)
    candidates = []
    seen = {(seed_artist, duplicate_text(seed.title, title=True))}
    for track in tracks:
        if track.id == seed.id or track.status != "ok" or not track.title:
            continue
        artist = duplicate_text(track.artist)
        if artist in {"unknown", "unknown artist", "various artists", "va"}:
            artist = ""
        shared = seed_genres & _genres(track.genre)
        same_artist = bool(seed_artist and seed_artist == artist)
        if not (shared or same_artist):
            continue
        key = (artist, duplicate_text(track.title, title=True))
        if not key[1]:
            continue
        candidates.append((len(shared) * 2 + int(same_artist), track, key))
    candidates.sort(key=lambda item: (-item[0], duplicate_text(item[1].artist),
                                      duplicate_text(item[1].title), item[1].id))
    result = []
    for _, track, key in candidates:
        if key in seen:
            continue
        if available and not available(track):
            continue
        seen.add(key)
        result.append(track)
        if len(result) >= limit:
            break
    return result
