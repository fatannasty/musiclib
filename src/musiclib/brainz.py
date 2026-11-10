from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.etree.ElementTree import ParseError

import httpx
import musicbrainzngs

from .config import Config
from .db import Database
from .duplicates import normalize
from .models import Album

OFFICIAL_STATUSES = {"Official", None, ""}
_request_lock = threading.Lock()
_last_request: Optional[float] = None
_user_agent = "musiclib/0.1"


def configure_musicbrainz(config: Config) -> None:
    global _user_agent
    contact = config.brainz.get("contact", "musiclib@example.com")
    _user_agent = f"musiclib/0.1 ({contact})"
    musicbrainzngs.set_useragent(
        "musiclib", "0.1", contact
    )
    musicbrainzngs.set_rate_limit(True)


@contextmanager
def musicbrainz_request(timeout: Optional[float] = None):
    global _last_request
    acquired = _request_lock.acquire() if timeout is None else _request_lock.acquire(timeout=timeout)
    if not acquired:
        raise musicbrainzngs.NetworkError("MusicBrainz is busy. Please retry the search.")
    try:
        if _last_request is not None:
            time.sleep(max(0.0, 1.0 - (time.monotonic() - _last_request)))
        _last_request = time.monotonic()
        yield
    finally:
        _request_lock.release()


def _search_catalog(entity: str, limit: int, terms: Dict[str, str]) -> Dict[str, Any]:
    # Preserve musicbrainzngs' non-strict field search without accepting Lucene operators.
    parts = []
    for name, value in terms.items():
        if value:
            escaped = re.sub(r'([+\-&|!(){}\[\]^"~*?:/\\])', r"\\\1", value)
            parts.append(f"{name}:({escaped.lower()})")
    query = " ".join(parts)
    if not query:
        raise ValueError("At least one search term is required.")
    try:
        with musicbrainz_request(timeout=4.0):
            response = httpx.get(
                f"https://musicbrainz.org/ws/2/{entity}/",
                params={"query": query, "limit": limit, "fmt": "xml"},
                headers={"User-Agent": _user_agent},
                timeout=4.0,
            )
            response.raise_for_status()
        payload = musicbrainzngs.mbxml.parse_message(response.content)
        if not isinstance(payload.get(f"{entity}-list"), list):
            raise musicbrainzngs.ResponseError("MusicBrainz returned an unexpected catalog response.")
        return payload
    except (httpx.HTTPError, OSError, ParseError) as exc:
        raise musicbrainzngs.NetworkError(f"Catalog request failed: {exc}") from exc


def search_recordings(limit: int = 6, **terms: str) -> Dict[str, Any]:
    return _search_catalog("recording", limit, terms)


def search_releases(limit: int = 6, **terms: str) -> Dict[str, Any]:
    return _search_catalog("release", limit, terms)


@dataclass
class ExpectedTrack:
    disc: int
    position: int
    title: str
    recording_mbid: Optional[str] = None


@dataclass
class AlbumGap:
    album: Album
    expected: List[ExpectedTrack] = field(default_factory=list)
    missing: List[ExpectedTrack] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)


class Brainz:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        configure_musicbrainz(config)

    def resolve_release(
        self, album_artist: str, album: str
    ) -> Optional[Tuple[str, int, int]]:
        try:
            with musicbrainz_request():
                result = musicbrainzngs.search_releases(
                    artist=album_artist, release=album, limit=10
                )
        except musicbrainzngs.WebServiceError:
            return None
        releases = result.get("release-list", [])
        if not releases:
            return None
        official = [r for r in releases if r.get("status") in OFFICIAL_STATUSES]
        pool = official or releases
        best = max(pool, key=lambda r: int(r.get("ext:score", 0)))
        mbid = best.get("id")
        if not mbid:
            return None
        year = None
        date = best.get("date") or ""
        if date[:4].isdigit():
            year = int(date[:4])
        return (mbid, year, len(best.get("medium-list", [])))

    def tracklist(self, mbid: str) -> List[ExpectedTrack]:
        try:
            with musicbrainz_request():
                result = musicbrainzngs.get_release_by_id(
                    mbid, includes=["recordings", "media"]
                )
        except musicbrainzngs.WebServiceError:
            return []
        release = result.get("release", {})
        tracks: List[ExpectedTrack] = []
        for disc_index, medium in enumerate(release.get("medium-list", []), start=1):
            position = medium.get("position") or disc_index
            for entry in medium.get("track-list", []):
                try:
                    track_position = int(entry.get("position") or 0)
                except (TypeError, ValueError):
                    track_position = 0
                recording = entry.get("recording", {})
                tracks.append(
                    ExpectedTrack(
                        disc=int(position),
                        position=track_position,
                        title=entry.get("title") or recording.get("title") or "",
                        recording_mbid=recording.get("id"),
                    )
                )
        return tracks

    def refresh_albums(
        self, force: bool = False,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[AlbumGap]:
        albums = self.db.albums_all()
        gaps: List[AlbumGap] = []
        for index, album in enumerate(albums, start=1):
            if album.mbid_verified and not force and album.missing_tracks is not None:
                if progress:
                    progress(index, len(albums), f"{album.album_artist} — {album.album}")
                continue
            resolved = None
            if not album.mbid_release or not album.mbid_verified or force:
                resolved = self.resolve_release(album.album_artist, album.album)
                if resolved:
                    album.mbid_release, year, _ = resolved
                    album.year = album.year or year
                    album.mbid_verified = True
                else:
                    album.mbid_verified = True
                    album.mbid_release = None
                    self.db.upsert_album(album)
                    if progress:
                        progress(index, len(albums), f"{album.album_artist} — {album.album}")
                    continue
            expected = self.tracklist(album.mbid_release)
            if not expected:
                self.db.upsert_album(album)
                if progress:
                    progress(index, len(albums), f"{album.album_artist} — {album.album}")
                continue
            album.expected_tracks = len(expected)
            missing, extra = self._compare(album, expected)
            album.missing_tracks = json.dumps(
                [
                    {"disc": t.disc, "position": t.position, "title": t.title}
                    for t in missing
                ]
            )
            self.db.upsert_album(album)
            gaps.append(AlbumGap(album=album, expected=expected, missing=missing, extra=extra))
            if progress:
                progress(index, len(albums), f"{album.album_artist} — {album.album}")
        return gaps

    def _compare(
        self, album: Album, expected: List[ExpectedTrack]
    ) -> Tuple[List[ExpectedTrack], List[str]]:
        rows = self.db.query(
            "SELECT track_no, disc_no, title FROM tracks WHERE "
            "LOWER(album_artist)=LOWER(?) AND LOWER(album)=LOWER(?) "
            "AND status IN ('ok','suspect')",
            (album.album_artist, album.album),
        )
        have_positions = {
            (row["disc_no"] or 1, row["track_no"]) for row in rows if row["track_no"]
        }
        have_titles = {normalize(row["title"]) for row in rows if row["title"]}
        missing: List[ExpectedTrack] = []
        for track in expected:
            if (track.disc, track.position) in have_positions:
                continue
            if track.title and normalize(track.title) in have_titles:
                continue
            missing.append(track)
        expected_titles = {normalize(t.title) for t in expected}
        extra = [
            row["title"]
            for row in rows
            if row["title"]
            and normalize(row["title"]) not in expected_titles
            and (row["disc_no"] or 1, row["track_no"]) not in {
                (t.disc, t.position) for t in expected
            }
        ]
        return missing, extra

    def albums_with_gaps(self) -> List[AlbumGap]:
        result: List[AlbumGap] = []
        for album in self.db.albums_all(
            "missing_tracks IS NOT NULL AND missing_tracks != '[]'"
        ):
            missing = [
                ExpectedTrack(
                    disc=m["disc"], position=m["position"], title=m["title"]
                )
                for m in json.loads(album.missing_tracks)
            ]
            result.append(
                AlbumGap(
                    album=album,
                    expected=[ExpectedTrack(0, 0, "")] * (album.expected_tracks or 0),
                    missing=missing,
                )
            )
        return result
