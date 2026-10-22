from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import musicbrainzngs

from .config import Config
from .db import Database
from .duplicates import normalize
from .models import Album

OFFICIAL_STATUSES = {"Official", None, ""}


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
        musicbrainzngs.set_useragent(
            "musiclib", "0.1", config.brainz.get("contact", "musiclib@example.com")
        )
        musicbrainzngs.set_rate_limit(True)

    def resolve_release(
        self, album_artist: str, album: str
    ) -> Optional[Tuple[str, int, int]]:
        try:
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

    def refresh_albums(self, force: bool = False) -> List[AlbumGap]:
        albums = self.db.albums_all()
        gaps: List[AlbumGap] = []
        for album in albums:
            if album.mbid_verified and not force and album.missing_tracks is not None:
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
                    continue
            expected = self.tracklist(album.mbid_release)
            if not expected:
                self.db.upsert_album(album)
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
