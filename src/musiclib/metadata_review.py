from __future__ import annotations

import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import musicbrainzngs
import mutagen

from .brainz import configure_musicbrainz
from .config import Config
from .db import Database
from .duplicates import normalize
from .fsops import is_inside
from .scanner import Scanner, ScanReport, file_hash, scan_worker

CAA_FRONT = "https://coverartarchive.org/release/{mbid}/front-250"
ITUNES_SEARCH = "https://itunes.apple.com/search"

EDITABLE_FIELDS = (
    "title", "artist", "album", "album_artist", "year",
    "track_no", "disc_no", "genre", "mbid_track", "mbid_release",
)
TAG_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "year": "date",
    "track_no": "tracknumber",
    "disc_no": "discnumber",
    "genre": "genre",
    "mbid_track": "musicbrainz_trackid",
    "mbid_release": "musicbrainz_albumid",
}


def _recording_candidate(recording: Dict[str, Any], track: Any) -> Dict[str, Any]:
    release = next(iter(_dict_list(recording.get("release-list"))), {})
    artist = _credit_name(recording.get("artist-credit")) or track.artist or ""
    album_artist = _credit_name(release.get("artist-credit")) or artist
    title = recording.get("title", "")
    try:
        score = int(recording.get("ext:score", 0))
    except (TypeError, ValueError):
        score = 0
    if normalize(title) == normalize(track.title):
        score += 5
    if normalize(artist) == normalize(track.artist):
        score += 5
    score = min(100, score)
    date = str(release.get("date", ""))
    year = int(date[:4]) if date[:4].isdigit() else None
    medium, position = {}, {}
    recording_id = recording.get("id")
    for candidate_medium in _dict_list(release.get("medium-list")):
        for candidate_track in _dict_list(candidate_medium.get("track-list")):
            candidate_recording = candidate_track.get("recording")
            entry_recording_id = (
                candidate_recording.get("id") if isinstance(candidate_recording, dict) else None
            )
            if (
                entry_recording_id == recording_id
                or normalize(candidate_track.get("title")) == normalize(title)
            ):
                medium, position = candidate_medium, candidate_track
                break
        if position:
            break
    release_mbid = release.get("id")
    return {
        "source": "MusicBrainz",
        "confidence": score,
        "recording_mbid": recording.get("id"),
        "release_mbid": release_mbid,
        "cover_url": CAA_FRONT.format(mbid=release_mbid) if release_mbid else None,
        "cover_fallback": None,
        "fields": {
            "title": title or None,
            "artist": artist or None,
            "album": release.get("title"),
            "album_artist": album_artist or None,
            "year": year,
            "track_no": position.get("position") if position else None,
            "disc_no": medium.get("position") if position else None,
            "genre": None,
            "mbid_track": recording.get("id"),
            "mbid_release": release_mbid,
        },
    }


def _dict_list(value: Any) -> List[Dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _credit_name(value: Any) -> str:
    names = []
    for item in _dict_list(value):
        name = item.get("name")
        if not isinstance(name, str):
            artist = item.get("artist")
            name = artist.get("name") if isinstance(artist, dict) else ""
        names.append(name if isinstance(name, str) else "")
    return "".join(names).strip()


def _candidate_key(candidate: Dict[str, Any]) -> Tuple[str, str, str]:
    fields = candidate.get("fields") or {}
    return (
        normalize(fields.get("title")),
        normalize(fields.get("artist")),
        normalize(fields.get("album")),
    )


def _itunes_artwork(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return url.replace("100x100bb", "300x300bb").replace("100x100", "300x300")


def _itunes_candidate(item: Dict[str, Any], track: Any) -> Optional[Dict[str, Any]]:
    title = item.get("trackName") if isinstance(item.get("trackName"), str) else ""
    artist = item.get("artistName") if isinstance(item.get("artistName"), str) else ""
    if not title:
        return None
    album = item.get("collectionName") if isinstance(item.get("collectionName"), str) else None
    album_artist = item.get("collectionArtistName") if isinstance(item.get("collectionArtistName"), str) else None
    genre = item.get("primaryGenreName") if isinstance(item.get("primaryGenreName"), str) else None
    year = None
    released = str(item.get("releaseDate") or "")
    if released[:4].isdigit():
        year = int(released[:4])
    score = 70
    if normalize(title) == normalize(track.title):
        score += 10
    if normalize(artist) == normalize(track.artist):
        score += 10
    return {
        "source": "iTunes",
        "confidence": min(100, score),
        "recording_mbid": None,
        "release_mbid": None,
        "cover_url": _itunes_artwork(item.get("artworkUrl100")),
        "cover_fallback": None,
        "fields": {
            "title": title or None,
            "artist": artist or None,
            "album": album,
            "album_artist": album_artist or artist or None,
            "year": year,
            "track_no": item.get("trackNumber"),
            "disc_no": item.get("discNumber"),
            "genre": genre,
            "mbid_track": None,
            "mbid_release": None,
        },
    }


def search_itunes(title: str, artist: str, track: Any) -> List[Dict[str, Any]]:
    term = " ".join(part for part in (artist, title) if part).strip()
    if not term:
        return []
    try:
        response = httpx.get(
            ITUNES_SEARCH,
            params={"term": term, "entity": "song", "limit": 5},
            headers={"User-Agent": "musiclib/0.1"},
            timeout=4.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, OSError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    candidates = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        candidate = _itunes_candidate(item, track)
        if candidate:
            candidates.append(candidate)
    return candidates


def search_musicbrainz(title: str, artist: str, track: Any) -> List[Dict[str, Any]]:
    terms = {}
    if title:
        terms["recording"] = title
    if artist:
        terms["artist"] = artist
    try:
        response = musicbrainzngs.search_recordings(limit=6, **terms)
    except musicbrainzngs.MusicBrainzError as exc:
        raise RuntimeError(f"MusicBrainz lookup failed: {exc}") from exc
    if not isinstance(response, dict) or not isinstance(response.get("recording-list", []), list):
        raise RuntimeError("MusicBrainz returned an unexpected response.")
    return [
        _recording_candidate(recording, track)
        for recording in _dict_list(response.get("recording-list"))
        if recording.get("id") and recording.get("title")
    ]


def _merge_candidates(
    musicbrainz: List[Dict[str, Any]], itunes: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    for candidate in musicbrainz:
        merged[_candidate_key(candidate)] = candidate
    for candidate in itunes:
        key = _candidate_key(candidate)
        existing = merged.get(key)
        if existing is None:
            extras.append(candidate)
            continue
        if candidate.get("cover_url"):
            existing["cover_fallback"] = existing.get("cover_url")
            existing["cover_url"] = candidate["cover_url"]
        if not existing["fields"].get("genre") and candidate["fields"].get("genre"):
            existing["fields"]["genre"] = candidate["fields"]["genre"]
        existing["source"] = "MusicBrainz + iTunes"
        existing["confidence"] = max(existing["confidence"], candidate["confidence"])
    return sorted([*merged.values(), *extras], key=lambda item: item["confidence"], reverse=True)


def lookup_track(
    track: Any, title: Optional[str] = None, artist: Optional[str] = None
) -> List[Dict[str, Any]]:
    search_title = ((track.title if title is None else title) or "").strip()
    search_artist = ((track.artist if artist is None else artist) or "").strip()
    if not search_title and not search_artist:
        raise ValueError("Enter a title or artist to search. File tags are unchanged.")
    musicbrainz_error = None
    musicbrainz_hits: List[Dict[str, Any]] = []
    itunes_hits: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        musicbrainz_future = pool.submit(search_musicbrainz, search_title, search_artist, track)
        itunes_future = pool.submit(search_itunes, search_title, search_artist, track)
        try:
            musicbrainz_hits = musicbrainz_future.result()
        except RuntimeError as exc:
            musicbrainz_error = exc
        itunes_hits = itunes_future.result()
    if not musicbrainz_hits and not itunes_hits:
        if musicbrainz_error:
            raise musicbrainz_error
        return []
    return _merge_candidates(musicbrainz_hits, itunes_hits)


class MetadataReview:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        configure_musicbrainz(config)
        db.execute(
            "CREATE TABLE IF NOT EXISTS metadata_reviews ("
            "id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL, path TEXT NOT NULL, "
            "file_hash TEXT NOT NULL, candidates TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, applied_at TEXT)"
        )

    def propose(
        self, track_id: int, title: Optional[str] = None, artist: Optional[str] = None
    ) -> Dict[str, Any]:
        track = self.db.get_track(track_id)
        if track is None or track.status == "deleted":
            raise ValueError("Track is not available in the library.")
        if not is_inside(track.path, self.config.library):
            raise ValueError("Track is outside the configured library.")
        if not os.path.isfile(track.path):
            raise ValueError("Track file is missing from disk.")
        candidates = lookup_track(track, title=title, artist=artist)
        cursor = self.db.execute(
            "INSERT INTO metadata_reviews "
            "(track_id,path,file_hash,candidates,status,created_at) VALUES (?,?,?,?,?,?)",
            (
                track_id, track.path, track.file_hash or "",
                json.dumps(candidates), "review", datetime.now(timezone.utc).isoformat(),
            ),
        )
        return {
            "review_id": cursor.lastrowid,
            "track": {
                "id": track.id, "path": track.path, "title": track.title,
                "artist": track.artist, "album_artist": track.album_artist,
                "album": track.album, "year": track.year, "track_no": track.track_no,
                "disc_no": track.disc_no, "genre": track.genre,
                "mbid_track": track.mbid_track, "mbid_release": track.mbid_release,
            },
            "candidates": candidates,
        }

    def apply(self, review_id: int, candidate_index: int, fields: List[str]) -> Dict[str, Any]:
        if not fields or set(fields) - set(EDITABLE_FIELDS):
            raise ValueError("Select one or more valid metadata fields.")
        row = self.db.query_one(
            "SELECT * FROM metadata_reviews WHERE id=? AND status='review'", (review_id,)
        )
        if row is None:
            raise ValueError("Metadata review is unavailable or already applied.")
        track = self.db.get_track(row["track_id"])
        if track is None or track.path != row["path"] or track.status == "deleted":
            raise ValueError("Track changed or is no longer available; run a new lookup.")
        if not is_inside(track.path, self.config.library):
            raise ValueError("Track is outside the configured library.")
        current_hash = track.file_hash or ""
        if not os.path.isfile(track.path):
            raise ValueError("Track file is missing from disk.")
        if current_hash != row["file_hash"] or file_hash(track.path) != row["file_hash"]:
            raise ValueError("Track changed since lookup; scan it and request a new review.")

        candidates = json.loads(row["candidates"])
        if candidate_index < 0 or candidate_index >= len(candidates):
            raise ValueError("Selected MusicBrainz candidate is unavailable.")
        candidate = candidates[candidate_index]
        tags_to_write = {
            TAG_KEYS[field]: candidate["fields"].get(field)
            for field in fields if candidate["fields"].get(field) not in (None, "")
        }
        if not tags_to_write:
            raise ValueError("The selected candidate has no values for the chosen fields.")

        path = Path(track.path)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.musiclib-", suffix=path.suffix, dir=path.parent
        )
        os.close(fd)
        try:
            shutil.copy2(path, temp_name)
            audio = mutagen.File(temp_name, easy=True)
            if audio is None:
                raise ValueError("Audio format does not support editable metadata.")
            if audio.tags is None:
                audio.add_tags()
            for key, value in tags_to_write.items():
                audio[key] = [str(value)]
            audio.save()
            result = scan_worker(temp_name)
            if result.get("error"):
                raise ValueError(f"Updated file could not be read: {result['error']}")
            result["path"] = str(path)
            os.replace(temp_name, path)
        except Exception:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise

        Scanner(self.config, self.db)._store_result(result, ScanReport(), row_exists=True)
        self.db.execute(
            "UPDATE metadata_reviews SET status='applied', applied_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), review_id),
        )
        return {"track_id": track.id, "path": str(path), "fields": tags_to_write}
