from __future__ import annotations

import os
import struct
import types
from pathlib import Path

import pytest

from musiclib.config import Config
from musiclib.db import Database

BITRATE_INDEX = {
    32: 1, 40: 2, 48: 3, 56: 4, 64: 5, 80: 6, 96: 7, 112: 8, 128: 9,
    160: 10, 192: 11, 224: 12, 256: 13, 320: 14,
}

PNG_500 = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 500, 500)
    + b"\x08\x06\x00\x00\x00" + b"\x00" * 64
)
JPEG_600x400 = (
    b"\xff\xd8\xff\xc0\x00\x0b\x08" + struct.pack(">HH", 400, 600) + b"\x00" * 32
)


def _flac_streaminfo(sample_rate: int, channels: int, bps: int, total: int) -> bytes:
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    return (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00"
        + b"\x00\x00\x00"
        + packed.to_bytes(8, "big")
        + b"\x00" * 16
    )


def _vorbis_comment(tags: dict) -> bytes:
    vendor = b"musiclib-tests"
    out = struct.pack("<I", len(vendor)) + vendor
    entries = [f"{k}={v}".encode() for k, v in tags.items()]
    out += struct.pack("<I", len(entries))
    for entry in entries:
        out += struct.pack("<I", len(entry)) + entry
    return out


def _picture_block(data: bytes, mime: bytes = b"image/jpeg") -> bytes:
    out = struct.pack(">I", 3)
    out += struct.pack(">I", len(mime)) + mime
    out += struct.pack(">I", 0)
    out += struct.pack(">IIII", 500, 500, 24, 0)
    out += struct.pack(">I", len(data)) + data
    return out


def _flac_block(block_type: int, body: bytes, last: bool) -> bytes:
    flag = 0x80 if last else 0x00
    return bytes([flag | block_type]) + len(body).to_bytes(3, "big") + body


def make_flac(
    path, tags=None, art=None, duration=180.0, sample_rate=44100, bps=16
):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    total = int(duration * sample_rate)
    blocks = [b"fLaC", _flac_block(0, _flac_streaminfo(sample_rate, 2, bps, total), False)]
    comment = _flac_block(4, _vorbis_comment(tags or {}), art is None)
    blocks.append(comment)
    if art is not None:
        blocks.append(_flac_block(6, _picture_block(art), True))
    path.write_bytes(b"".join(blocks))
    return str(path)


def make_mp3(path, tags=None, bitrate=128, seconds=12.0):
    from mutagen.id3 import TALB, TDRC, TIT2, TPE1, TPE2, TRCK
    from mutagen.mp3 import MP3

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    idx = BITRATE_INDEX[bitrate]
    frame_len = int(144 * bitrate * 1000 / 44100)
    frame_count = int(seconds * 44100 / 1152) + 1
    with open(path, "wb") as fh:
        for _ in range(frame_count):
            fh.write(bytes([0xFF, 0xFB, idx << 4, 0x00]) + b"\x00" * (frame_len - 4))
    audio = MP3(str(path))
    audio.add_tags()
    frame_classes = {
        "title": TIT2, "artist": TPE1, "album": TALB,
        "albumartist": TPE2, "tracknumber": TRCK, "date": TDRC,
    }
    for key, value in (tags or {}).items():
        frame_cls = frame_classes.get(key.lower())
        if frame_cls:
            audio.tags.add(frame_cls(encoding=3, text=str(value)))
    audio.save()
    return str(path)


def make_wav(path, seconds=5.0):
    import wave

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\x00\x00\x00\x00" * int(seconds * 44100))
    return str(path)


ALBUM_A = {
    "ARTIST": "Artist One",
    "ALBUMARTIST": "Artist One",
    "ALBUM": "Album A",
    "DATE": "2001",
    "MUSICBRAINZ_ALBUMID": "mbid-album-a",
}


@pytest.fixture
def lib_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "musiclib.config.DEFAULT_DB_PATH", tmp_path / "data" / "library.db"
    )
    library = tmp_path / "Music"
    library.mkdir()
    config = Config.create(str(library), path=tmp_path / "config.toml")
    config.auth_required = False
    db = Database(config.db_path)
    env = types.SimpleNamespace(config=config, db=db, library=library, tmp=tmp_path)
    yield env
    db.close()


@pytest.fixture
def mini_library(lib_env):
    lib = lib_env.library
    make_flac(
        lib / "Artist One" / "Album A (2001)" / "01 - First Song.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"},
        art=JPEG_600x400,
        duration=12.0,
    )
    make_mp3(
        lib / "downloads" / "Artist One - First Song.mp3",
        tags={
            "title": "First Song", "artist": "Artist One", "album": "Album A",
            "albumartist": "Artist One", "tracknumber": "1", "date": "2001",
        },
        bitrate=320,
    )
    (lib / "Artist One" / "Album A (2001)" / "01 - First Song (copy).flac").write_bytes(
        (lib / "Artist One" / "Album A (2001)" / "01 - First Song.flac").read_bytes()
    )
    make_mp3(
        lib / "Artist One" / "Album A (2001)" / "02 - Second Song.mp3",
        tags={**ALBUM_A, "TITLE": "Second Song", "TRACKNUMBER": "2"},
        bitrate=128,
    )
    (lib / "broken").mkdir(parents=True, exist_ok=True)
    (lib / "broken" / "noise.flac").write_bytes(os.urandom(2048))
    make_wav(lib / "untagged" / "mystery.wav")
    return lib_env
