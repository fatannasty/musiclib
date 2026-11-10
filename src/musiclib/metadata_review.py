from __future__ import annotations

import json
import logging
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

from .brainz import configure_musicbrainz, search_recordings, search_releases
from .config import Config
from .db import Database
from .duplicates import normalize
from .fsops import is_inside
from .models import Track
from .scanner import Scanner, ScanReport, file_hash, scan_worker

CAA_FRONT = "https://coverartarchive.org/release/{mbid}/front-250"
ITUNES_SEARCH = "https://itunes.apple.com/search"
logger = logging.getLogger(__name__)

EDITABLE_FIELDS = (
    "title", "artist", "album", "album_artist", "year",
    "track_no", "disc_no", "genre", "mbid_track", "mbid_release",
)
ALBUM_FIELDS = ("album", "album_artist", "artist", "year", "genre", "mbid_release")
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
    except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"iTunes lookup failed: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("iTunes returned an unexpected response.")
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
        response = search_recordings(limit=6, **terms)
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
    track: Any, title: Optional[str] = None, artist: Optional[str] = None,
    warnings: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    search_title = ((track.title if title is None else title) or "").strip()
    search_artist = ((track.artist if artist is None else artist) or "").strip()
    if not search_title and not search_artist:
        raise ValueError("Enter a title or artist to search. File tags are unchanged.")
    errors = []
    musicbrainz_hits: List[Dict[str, Any]] = []
    itunes_hits: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        musicbrainz_future = pool.submit(search_musicbrainz, search_title, search_artist, track)
        itunes_future = pool.submit(search_itunes, search_title, search_artist, track)
        try:
            musicbrainz_hits = musicbrainz_future.result()
        except RuntimeError as exc:
            errors.append(str(exc))
        try:
            itunes_hits = itunes_future.result()
        except RuntimeError as exc:
            errors.append(str(exc))
    for error in errors:
        logger.warning("%s", error)
    if warnings is not None:
        warnings.extend(errors)
    if not musicbrainz_hits and not itunes_hits:
        if errors:
            raise RuntimeError("; ".join(errors))
        return []
    return _merge_candidates(musicbrainz_hits, itunes_hits)


def write_audio_tags(path: Path, tags_to_write: Dict[str, Any]) -> Dict[str, Any]:
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
        return result
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def _most_common(values: List[Any]) -> Any:
    counted: Dict[Any, int] = {}
    for value in values:
        if value in (None, ""):
            continue
        counted[value] = counted.get(value, 0) + 1
    if not counted:
        return None
    return max(counted, key=counted.get)


def search_musicbrainz_releases(artist: str, album: str) -> List[Dict[str, Any]]:
    terms = {}
    if album:
        terms["release"] = album
    if artist:
        terms["artist"] = artist
    if not terms:
        return []
    try:
        response = search_releases(limit=6, **terms)
    except musicbrainzngs.MusicBrainzError as exc:
        raise RuntimeError(f"MusicBrainz album lookup failed: {exc}") from exc
    if not isinstance(response, dict) or not isinstance(response.get("release-list"), list):
        raise RuntimeError("MusicBrainz returned an unexpected album response.")
    suggestions = []
    for release in _dict_list(response.get("release-list")):
        title = release.get("title")
        if not title or not release.get("id"):
            continue
        credit = _credit_name(release.get("artist-credit")) or artist
        date = str(release.get("date") or "")
        year = int(date[:4]) if date[:4].isdigit() else None
        try:
            score = int(release.get("ext:score", 0))
        except (TypeError, ValueError):
            score = 0
        suggestions.append({
            "source": "MusicBrainz",
            "confidence": min(100, score),
            "cover_url": CAA_FRONT.format(mbid=release["id"]),
            "cover_fallback": None,
            "fields": {
                "album": title,
                "album_artist": credit or None,
                "artist": credit or None,
                "year": year,
                "genre": None,
                "mbid_release": release["id"],
            },
        })
    return suggestions


def search_itunes_albums(artist: str, album: str) -> List[Dict[str, Any]]:
    term = " ".join(part for part in (artist, album) if part).strip()
    if not term:
        return []
    try:
        response = httpx.get(
            ITUNES_SEARCH,
            params={"term": term, "entity": "album", "limit": 5},
            headers={"User-Agent": "musiclib/0.1"},
            timeout=4.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"iTunes album lookup failed: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("iTunes returned an unexpected album response.")
    suggestions = []
    for item in payload["results"]:
        if not isinstance(item, dict) or not isinstance(item.get("collectionName"), str):
            continue
        credit = item.get("artistName") if isinstance(item.get("artistName"), str) else artist
        released = str(item.get("releaseDate") or "")
        year = int(released[:4]) if released[:4].isdigit() else None
        genre = item.get("primaryGenreName") if isinstance(item.get("primaryGenreName"), str) else None
        suggestions.append({
            "source": "iTunes",
            "confidence": 75,
            "cover_url": _itunes_artwork(item.get("artworkUrl100")),
            "cover_fallback": None,
            "fields": {
                "album": item.get("collectionName"),
                "album_artist": credit or None,
                "artist": credit or None,
                "year": year,
                "genre": genre,
                "mbid_release": None,
            },
        })
    return suggestions


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
        warnings: List[str] = []
        candidates = lookup_track(track, title=title, artist=artist, warnings=warnings)
        return {**self._save_review(track, candidates), "warnings": warnings}

    def _save_review(self, track: Track, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        cursor = self.db.execute(
            "INSERT INTO metadata_reviews "
            "(track_id,path,file_hash,candidates,status,created_at) VALUES (?,?,?,?,?,?)",
            (
                track.id, track.path, track.file_hash or "",
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

    def preview_album_edit(
        self, track_ids: List[int], fields: List[str], values: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not fields or set(fields) - set(ALBUM_FIELDS) or len(set(fields)) != len(fields):
            raise ValueError("Select one or more distinct album fields to edit.")
        selected: Dict[str, Any] = {}
        for field in fields:
            value = values.get(field)
            if field == "year":
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise ValueError("Year must be a whole number between 1 and 9999.")
                text = str(value).strip()
                if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 9999:
                    raise ValueError("Year must be a whole number between 1 and 9999.")
                selected[field] = int(text)
            else:
                if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                    raise ValueError(f"Enter a non-empty value of at most 1000 characters for {field}.")
                selected[field] = value.strip()
        tracks = self._tracks_for_album(track_ids)
        reviews, errors = [], []
        for track in tracks:
            if not os.path.isfile(track.path) or not track.file_hash:
                errors.append({
                    "track_id": track.id, "path": track.path,
                    "error": "File is missing or has not been hashed. Scan it before editing.",
                })
                continue
            reviews.append(self._save_review(track, [{
                "source": "Manual edit", "fields": selected,
            }]))
        return {"reviews": reviews, "errors": errors, "fields": fields}

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
        result = write_audio_tags(Path(track.path), tags_to_write)
        Scanner(self.config, self.db)._store_result(result, ScanReport(), row_exists=True)
        self.db.execute(
            "UPDATE metadata_reviews SET status='applied', applied_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), review_id),
        )
        return {"track_id": track.id, "path": track.path, "fields": tags_to_write}

    def _tracks_for_album(self, track_ids: List[int]) -> List[Any]:
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise ValueError("Select between 1 and 200 distinct tracks.")
        tracks = []
        for track_id in track_ids:
            track = self.db.get_track(track_id)
            if track is None or track.status == "deleted":
                raise ValueError("A selected track is not available in the library.")
            if not is_inside(track.path, self.config.library):
                raise ValueError("A selected track is outside the configured library.")
            tracks.append(track)
        return tracks

    def suggest_album(
        self,
        track_ids: List[int],
        album: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> Dict[str, Any]:
        tracks = self._tracks_for_album(track_ids)
        album_guess = (album or "").strip() or _most_common([track.album for track in tracks]) or ""
        artist_guess = (
            (artist or "").strip()
            or _most_common([track.album_artist or track.artist for track in tracks])
            or ""
        )
        if not album_guess and not artist_guess:
            raise ValueError("Enter an album or artist to search. File tags are unchanged.")
        artists = {
            normalize(track.artist) for track in tracks if track.artist
        } | {
            normalize(track.album_artist) for track in tracks if track.album_artist
        }
        if artist_guess:
            artists.add(normalize(artist_guess))
        rows = self.db.query(
            "SELECT album_artist, artist, album, year, genre, COUNT(*) AS n FROM tracks "
            "WHERE status IN ('ok', 'suspect') AND album IS NOT NULL AND album != '' "
            "GROUP BY COALESCE(album_artist, artist), album, year ORDER BY n DESC LIMIT 250"
        )
        library = []
        for row in rows:
            owner = row["album_artist"] or row["artist"] or ""
            score = 0
            if normalize(owner) in artists:
                score += 55
            if album_guess and normalize(row["album"]) == normalize(album_guess):
                score += 35
            elif album_guess and normalize(album_guess) in normalize(row["album"]):
                score += 15
            if score < 55:
                continue
            library.append({
                "source": "Your library",
                "confidence": min(100, score + min(10, row["n"])),
                "cover_url": None,
                "cover_fallback": None,
                "fields": {
                    "album": row["album"],
                    "album_artist": row["album_artist"] or owner or None,
                    "artist": row["artist"] or owner or None,
                    "year": row["year"],
                    "genre": row["genre"],
                    "mbid_release": None,
                },
                "in_library": row["n"],
            })
        library.sort(key=lambda item: (-item["confidence"], -item["in_library"]))
        catalogs: List[Dict[str, Any]] = []
        warnings = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            brainz_future = pool.submit(search_musicbrainz_releases, artist_guess, album_guess)
            itunes_future = pool.submit(search_itunes_albums, artist_guess, album_guess)
            for future in (brainz_future, itunes_future):
                try:
                    catalogs.extend(future.result())
                except RuntimeError as exc:
                    warnings.append(str(exc))
                    logger.warning("%s", exc)
        if not library and not catalogs and warnings:
            raise RuntimeError("; ".join(warnings))
        catalogs.sort(key=lambda item: item["confidence"], reverse=True)
        return {
            "tracks": [
                {
                    "id": track.id, "path": track.path, "title": track.title,
                    "artist": track.artist, "album": track.album,
                    "album_artist": track.album_artist, "year": track.year,
                    "file_hash": track.file_hash or "",
                }
                for track in tracks
            ],
            "query": {"album": album_guess, "artist": artist_guess},
            "suggestions": library[:8] + catalogs[:8],
            "warnings": warnings,
        }

    def apply_album(
        self,
        track_ids: List[int],
        fields: List[str],
        values: Dict[str, Any],
        rename: bool = False,
        snapshots: Optional[Dict[int, Tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        if not fields or set(fields) - set(ALBUM_FIELDS):
            raise ValueError("Select one or more album fields to apply.")
        if not 1 <= len(track_ids) <= 200 or len(set(track_ids)) != len(track_ids):
            raise ValueError("Select between 1 and 200 distinct tracks.")
        if snapshots is not None and set(snapshots) != set(track_ids):
            raise ValueError("Search snapshots must cover exactly the selected tracks.")
        tags_to_write = {
            TAG_KEYS[field]: values.get(field)
            for field in fields if values.get(field) not in (None, "")
        }
        if not tags_to_write:
            raise ValueError("The selected album has no values for the chosen fields.")
        applied, errors = [], []
        for track_id in track_ids:
            track = self.db.get_track(track_id)
            if track is None or track.status == "deleted":
                errors.append({"track_id": track_id, "error": "Track is not available."})
                continue
            try:
                if not is_inside(track.path, self.config.library):
                    raise ValueError("Track is outside the configured library.")
                if not os.path.isfile(track.path):
                    raise ValueError("Track file is missing from disk.")
                if snapshots is not None and (
                    not track.file_hash
                    or snapshots[track_id] != (track.path, track.file_hash)
                    or file_hash(Path(track.path)) != track.file_hash
                ):
                    raise ValueError("File changed since album search. Run a fresh search.")
                result = write_audio_tags(Path(track.path), tags_to_write)
                Scanner(self.config, self.db)._store_result(result, ScanReport(), row_exists=True)
                applied.append({"track_id": track.id, "path": track.path})
            except (ValueError, OSError, mutagen.MutagenError) as exc:
                errors.append({"track_id": track.id, "path": track.path, "error": str(exc)})
        renamed, rename_errors = 0, []
        if rename and applied:
            from .rename import Renamer
            selected = [self.db.get_track(item["track_id"]) for item in applied]
            selected = [track for track in selected if track is not None]
            report = Renamer(self.config, self.db).apply(Renamer(self.config, self.db).plan(selected))
            renamed = report.moved
            rename_errors = [{"path": path, "error": reason} for path, reason in report.errors]
        return {
            "applied": applied, "errors": errors, "fields": tags_to_write,
            "renamed": renamed, "rename_errors": rename_errors,
        }
