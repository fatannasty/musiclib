from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List
from uuid import UUID

import musicbrainzngs

from . import brainz
from .catalog_http import catalog_error
from .config import Config
from .db import Database
from .duplicates import normalize


def _release_tracks(release_id: str, ipv4_only: bool) -> List[Dict[str, Any]]:
    try:
        release_id = str(UUID(release_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("MusicBrainz returned an invalid release ID.") from exc
    payload = brainz.get_release(release_id, ipv4_only=ipv4_only)
    tracks = []
    for medium_index, medium in enumerate(payload["media"], 1):
        if not isinstance(medium, dict) or not isinstance(medium.get("tracks"), list):
            raise RuntimeError("MusicBrainz returned an incomplete release track list.")
        count = medium.get("track-count")
        if isinstance(count, int) and count != len(medium["tracks"]):
            raise RuntimeError("MusicBrainz returned an incomplete release track list.")
        for entry in medium["tracks"]:
            if not isinstance(entry, dict):
                raise RuntimeError("MusicBrainz returned an invalid track.")
            recording = entry.get("recording") or {}
            if not isinstance(recording, dict):
                raise RuntimeError("MusicBrainz returned an invalid recording.")
            duration = entry.get("length")
            if duration is None:
                duration = recording.get("length")
            tracks.append({
                "disc": medium.get("position") or medium_index,
                "position": entry.get("position"),
                "title": entry.get("title") or recording.get("title") or "",
                "duration": duration / 1000 if isinstance(duration, (int, float)) and duration >= 0 else None,
            })
    if not tracks:
        raise RuntimeError("MusicBrainz has no track list for this edition.")
    return tracks


def compare_tracks(local: List[Dict[str, Any]], expected: List[Dict[str, Any]]) -> Dict[str, Any]:
    local_names = Counter(normalize(track["title"]) for track in local if track["title"])
    expected_names = Counter(normalize(track["title"]) for track in expected if track["title"])
    local_by_name = {
        normalize(track["title"]): track for track in local
        if track["title"] and local_names[normalize(track["title"])] == 1
    }
    matched = set()
    comparisons = []
    for track in expected:
        name = normalize(track["title"]) if track["title"] else ""
        match = local_by_name.get(name) if name and expected_names[name] == 1 else None
        flags = []
        if match:
            matched.add(match["id"])
            if match["track_no"] and track["position"] and (
                (match["disc_no"] or 1, match["track_no"]) != (track["disc"], track["position"])
            ):
                flags.append("Different disc or track number")
            if match["duration"] is not None and track["duration"] is not None and abs(
                match["duration"] - track["duration"]
            ) > 5:
                flags.append("Duration differs by more than 5 seconds")
        else:
            flags.append("No unique title match; possibly missing or named differently")
        comparisons.append({"catalog": track, "local": match, "flags": flags})
    return {
        "tracks": comparisons,
        "unmatched_local": [track for track in local if track["id"] not in matched],
    }


def audit_album(
    config: Config, db: Database, artist: str, album: str,
    progress: Callable[..., None], check_cancelled: Callable[[], None],
) -> Dict[str, Any]:
    rows = db.album_tracks(artist, album)
    if not rows:
        raise ValueError("This album is no longer available in the library.")
    if len(rows) > 200:
        raise ValueError("This album has more than 200 tracks; it cannot be compared in this review.")
    local = []
    for row in rows:
        track = db.get_track(row["id"])
        if track is None:
            raise ValueError("Album tracks changed during the comparison. Search again.")
        local.append({
            "id": track.id, "path": track.path, "title": track.title or "",
            "artist": track.artist or "", "track_no": track.track_no,
            "disc_no": track.disc_no, "duration": track.duration, "status": track.status,
        })
    brainz.configure_musicbrainz(config)
    ipv4 = bool(config.catalog.get("ipv4_only", False))
    try:
        response = brainz.search_releases(
            limit=6, artist=artist, release=album, ipv4_only=ipv4,
        )
    except musicbrainzngs.MusicBrainzError as exc:
        raise RuntimeError(f"MusicBrainz album search failed: {catalog_error(exc)}") from exc
    releases = response.get("release-list") if isinstance(response, dict) else None
    if not isinstance(releases, list):
        raise RuntimeError("MusicBrainz returned an unexpected album search response.")
    editions = []
    for index, release in enumerate(releases[:6], 1):
        check_cancelled()
        if not isinstance(release, dict) or not release.get("id"):
            raise RuntimeError("MusicBrainz returned an invalid release.")
        item: Dict[str, Any] = {
            "id": release["id"], "title": release.get("title") or album,
            "date": release.get("date") or "", "country": release.get("country") or "",
            "status": release.get("status") or "", "source": "MusicBrainz",
        }
        progress("Comparing MusicBrainz editions", index - 1, len(releases[:6]), item["title"])
        try:
            expected = _release_tracks(item["id"], ipv4)
            item.update(compare_tracks(local, expected))
        except RuntimeError as exc:
            item["error"] = str(exc)
        editions.append(item)
        progress("Comparing MusicBrainz editions", index, len(releases[:6]), item["title"])
    return {"album_artist": artist, "album": album, "local_count": len(local), "editions": editions}
