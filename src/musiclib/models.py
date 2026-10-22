from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

AUDIO_EXTENSIONS = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".m4a": "mp4",
    ".alac.m4a": "alac",
    ".aac": "aac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "opus",
    ".wma": "wma",
    ".wav": "wav",
    ".aiff": "aiff",
    ".aif": "aiff",
    ".ape": "ape",
    ".wv": "wv",
}

LOSSLESS_FORMATS = {"flac", "alac", "wav", "aiff", "ape", "wv"}


def audio_format(path: str) -> Optional[str]:
    lowered = path.lower()
    if lowered.endswith(".alac.m4a"):
        return "alac"
    ext = lowered.rsplit(".", 1)[-1]
    fmt = AUDIO_EXTENSIONS.get("." + ext)
    if ext == "m4a":
        return "mp4"
    return fmt


@dataclass
class Track:
    id: Optional[int] = None
    path: str = ""
    mtime: float = 0.0
    size: int = 0
    format: Optional[str] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None
    duration: Optional[float] = None
    artist: Optional[str] = None
    album_artist: Optional[str] = None
    album: Optional[str] = None
    title: Optional[str] = None
    track_no: Optional[int] = None
    track_total: Optional[int] = None
    disc_no: Optional[int] = None
    disc_total: Optional[int] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    mbid_track: Optional[str] = None
    mbid_release: Optional[str] = None
    has_embedded_art: bool = False
    file_hash: Optional[str] = None
    fingerprint: Optional[str] = None
    quality_score: int = 0
    status: str = "ok"
    issues: list = field(default_factory=list)


@dataclass
class Issue:
    kind: str
    detail: str
    severity: str = "warn"

    def to_row(self) -> tuple:
        return (self.kind, self.detail, self.severity)


@dataclass
class Album:
    id: Optional[int] = None
    album_artist: str = ""
    album: str = ""
    year: Optional[int] = None
    mbid_release: Optional[str] = None
    mbid_verified: bool = False
    art_status: str = "unknown"
    expected_tracks: Optional[int] = None
    have_tracks: int = 0
    missing_tracks: Optional[str] = None

    @property
    def key(self) -> tuple:
        return (self.album_artist.lower(), self.album.lower())


@dataclass
class IndexerResult:
    indexer: str
    title: str
    url: str
    size: int = 0
    posted: Optional[str] = None
    score: int = 0
