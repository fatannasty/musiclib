from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import mutagen
from mutagen.flac import Picture

from .config import Config
from .db import Database
from .models import Album

CAA_BASE = "https://coverartarchive.org"

FOLDER_IMAGE_CANDIDATES = [
    "folder.jpg", "folder.png", "cover.jpg", "cover.png", "front.jpg", "front.png",
]


def image_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height)
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA,
                          0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return (width, height)
            offset += 2 + length
    return None


def image_mime(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


@dataclass
class ArtResult:
    album: str
    artist: str
    status: str
    detail: str = ""
    bytes_written: int = 0
    embedded: int = 0


class Artwork:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.folder_image = str(config.artwork.get("folder_image", "folder.jpg"))
        self.min_side = int(config.artwork.get("min_side", 300))
        self.embed_enabled = bool(config.artwork.get("embed", True))
        self.client = httpx.Client(
            headers={"User-Agent": f"musiclib/0.1 ( {config.brainz.get('contact', '')} )"},
            follow_redirects=True,
            timeout=30,
        )

    def albums_needing_art(self, replace_small: bool = False) -> List[Album]:
        statuses = ("missing", "none", "unknown") if not replace_small else (
            "missing", "none", "unknown", "small"
        )
        placeholders = ",".join("?" for _ in statuses)
        return self.db.albums_all(f"art_status IN ({placeholders})", statuses)

    def album_tracks(self, album: Album) -> List:
        return self.db.query(
            "SELECT * FROM tracks WHERE LOWER(album_artist)=LOWER(?) AND "
            "LOWER(album)=LOWER(?) AND status IN ('ok','suspect') ORDER BY path",
            (album.album_artist, album.album),
        )

    def download_front(self, mbid: str) -> Optional[bytes]:
        for url in (
            f"{CAA_BASE}/release/{mbid}/front-2500",
            f"{CAA_BASE}/release/{mbid}/front-500",
            f"{CAA_BASE}/release-group/{mbid}/front-2500",
        ):
            try:
                response = self.client.get(url)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            data = response.content
            if not image_mime(data) or len(data) < 5000:
                continue
            return data
        return None

    def process_album(
        self, album: Album, mbid_resolver, replace_small: bool = False
    ) -> ArtResult:
        tracks = self.album_tracks(album)
        if not tracks:
            return ArtResult(album.album, album.album_artist, "skipped", "no tracks")
        if not album.mbid_release or not album.mbid_verified:
            resolved = mbid_resolver(album.album_artist, album.album)
            if resolved:
                album.mbid_release = resolved[0]
                album.mbid_verified = True
            else:
                album.art_status = "none"
                album.mbid_verified = True
                self.db.upsert_album(album)
                return ArtResult(
                    album.album, album.album_artist, "none", "no MusicBrainz match"
                )
        data = self.download_front(album.mbid_release)
        if not data:
            album.art_status = "failed"
            self.db.upsert_album(album)
            return ArtResult(
                album.album, album.album_artist, "failed", "no cover on Cover Art Archive"
            )
        dims = image_dimensions(data)
        if dims and min(dims) < self.min_side:
            album.art_status = "small"
            self.db.upsert_album(album)
            return ArtResult(
                album.album, album.album_artist, "small",
                f"art {dims[0]}x{dims[1]} below {self.min_side}px",
            )
        album_dir = Path(tracks[0]["path"]).parent
        folder_path = album_dir / self.folder_image
        try:
            folder_path.write_bytes(data)
        except OSError as exc:
            return ArtResult(album.album, album.album_artist, "error", str(exc))
        embedded = 0
        if self.embed_enabled:
            for row in tracks:
                if self._embed(row["path"], data):
                    embedded += 1
                    self.db.update_track_columns(row["id"], has_embedded_art=1)
        album.art_status = "downloaded"
        self.db.upsert_album(album)
        return ArtResult(
            album.album, album.album_artist, "downloaded",
            f"{dims[0]}x{dims[1]}" if dims else "",
            bytes_written=len(data), embedded=embedded,
        )

    def _embed(self, path: str, data: bytes) -> bool:
        mime = image_mime(data) or "image/jpeg"
        try:
            audio = mutagen.File(path, easy=False)
            if audio is None:
                return False
            fmt = audio.__class__.__name__
            if fmt in ("FLAC", "OggVorbis", "OggOpus"):
                picture = Picture()
                picture.type = 3
                picture.mime = mime
                picture.desc = "Cover"
                picture.data = data
                if fmt == "FLAC":
                    audio.clear_pictures()
                    audio.add_picture(picture)
                else:
                    audio["metadata_block_picture"] = [
                        base64.b64encode(picture.write()).decode("ascii")
                    ]
                audio.save()
                return True
            if fmt in ("MP3", "AIFF", "WAVE"):
                from mutagen.id3 import APIC, ID3

                if audio.tags is None:
                    audio.tags = ID3()
                audio.tags.add(
                    APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data)
                )
                audio.save()
                return True
            if fmt == "MP4":
                from mutagen.mp4 import MP4, MP4Cover

                mp4 = MP4(path)
                imageformat = (
                    MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                )
                mp4["covr"] = [MP4Cover(data, imageformat=imageformat)]
                mp4.save()
                return True
            if fmt == "ASF":
                from mutagen.asf import ASFPicture

                audio["WM/Picture"] = [ASFPicture(data=data, mime=mime, type=3)]
                audio.save()
                return True
            return False
        except Exception:
            return False
