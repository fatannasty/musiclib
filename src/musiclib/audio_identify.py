from __future__ import annotations

import logging
from typing import Any, Dict, List
from uuid import UUID

import httpx

from .brainz import get_recording
from .catalog_http import catalog_error, catalog_post
from .fingerprint import compute_encoded_fingerprint

logger = logging.getLogger(__name__)
ACOUSTID_LOOKUP = "https://api.acoustid.org/v2/lookup"
MAX_RECORDINGS = 5


def identify_audio(
    path: str, api_key: str, *, ipv4_only: bool = False,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("Add your AcoustID application API key in Settings before identifying audio.")
    fingerprint = compute_encoded_fingerprint(path)
    try:
        response = catalog_post(
            ACOUSTID_LOOKUP,
            data={
                "client": api_key, "format": "json", "meta": "recordingids",
                "duration": str(fingerprint["duration"]),
                "fingerprint": fingerprint["fingerprint"],
            },
            headers={"User-Agent": "musiclib/0.1"},
            timeout=10.0, ipv4_only=ipv4_only,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise RuntimeError(f"AcoustID lookup failed: {catalog_error(exc)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AcoustID returned an unexpected response.")
    if payload.get("status") != "ok":
        raise RuntimeError(
            "AcoustID rejected the lookup. Check the application API key in Settings "
            "and the service's availability. No tags were changed."
        )
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("AcoustID returned an unexpected response.")
    recordings: Dict[str, float] = {}
    for result in results:
        if not isinstance(result, dict):
            raise RuntimeError("AcoustID returned an invalid match.")
        score = result.get("score")
        if (
            not isinstance(score, (int, float)) or isinstance(score, bool)
            or not 0 <= score <= 1
        ):
            raise RuntimeError("AcoustID returned an invalid match score.")
        linked = result.get("recordings", [])
        if not isinstance(linked, list):
            raise RuntimeError("AcoustID returned invalid recording links.")
        for recording in linked:
            if not isinstance(recording, dict) or not isinstance(recording.get("id"), str):
                raise RuntimeError("AcoustID returned an invalid recording ID.")
            try:
                recording_id = str(UUID(recording["id"]))
            except ValueError as exc:
                raise RuntimeError("AcoustID returned an invalid recording ID.") from exc
            recordings[recording_id] = max(score, recordings.get(recording_id, 0))
    if results and not recordings:
        warnings.append("AcoustID found audio matches but no linked MusicBrainz recordings.")
    candidates = []
    errors = []
    for recording_id, score in sorted(recordings.items(), key=lambda item: -item[1])[:MAX_RECORDINGS]:
        try:
            recording = get_recording(recording_id, ipv4_only=ipv4_only)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        names = []
        for credit in recording["artist-credit"]:
            if not isinstance(credit, dict):
                continue
            artist = credit.get("artist")
            name = credit.get("name") or (artist.get("name") if isinstance(artist, dict) else None)
            if isinstance(name, str):
                joinphrase = credit.get("joinphrase")
                names.append(name + (joinphrase if isinstance(joinphrase, str) else ""))
        candidates.append({
            "source": "AcoustID + MusicBrainz", "confidence": round(score * 100),
            "source_url": f"https://musicbrainz.org/recording/{recording_id}",
            "recording_mbid": recording_id, "release_mbid": None,
            "cover_url": None, "cover_fallback": None,
            "evidence": (
                "Audio fingerprint match; the score is not a guarantee of identity. "
                "A recording can appear on many releases: album, year and track numbers "
                "are deliberately not guessed."
            ),
            "fields": {
                "title": recording["title"], "artist": "".join(names).strip() or None,
                "mbid_track": recording_id,
            },
        })
    for error in dict.fromkeys(errors):
        logger.warning("%s", error)
        warnings.append(error)
    if recordings and not candidates and errors:
        raise RuntimeError("; ".join(dict.fromkeys(errors)))
    return candidates
