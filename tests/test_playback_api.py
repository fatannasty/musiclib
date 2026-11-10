import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_mp3, make_wav
from fastapi import HTTPException
from fastapi.testclient import TestClient

import musiclib.server.playback as playback_module
from musiclib.models import Track, audio_format
from musiclib.scanner import Scanner
from musiclib.server.app import create_app
from musiclib.server.playback import Playback


def catalog_file(env, path, status="ok"):
    return env.db.upsert_track(Track(
        path=str(path), format=audio_format(str(path)), duration=2,
        status=status, title=path.stem,
    ))


def test_original_audio_metadata_and_range_seeking(lib_env):
    env = lib_env
    path = env.library / "listen.mp3"
    make_mp3(path, tags={"title": "Quality check", "artist": "Artist"}, seconds=2)
    Scanner(env.config, env.db, workers=1).scan()
    track = env.db.find_track_by_path(str(path))
    data = path.read_bytes()
    with TestClient(create_app(env.config)) as client:
        info = client.get(f"/api/tracks/{track.id}/playback").json()
        assert info["title"] == "Quality check"
        assert info["artist"] == "Artist"
        assert info["sample_rate"] == 44100
        assert info["media_type"] == "audio/mpeg"
        response = client.get(info["original_url"])
        assert response.status_code == 200
        assert response.content == data
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["cache-control"] == "no-store"
        for requested, expected in (("bytes=100-199", data[100:200]), ("bytes=-50", data[-50:])):
            response = client.get(info["original_url"], headers={"Range": requested})
            assert response.status_code == 206
            assert response.content == expected
            assert "content-range" in response.headers
        assert client.get(
            info["original_url"], headers={"Range": f"bytes={len(data) + 1}-"},
        ).status_code == 416
    assert path.read_bytes() == data
    assert env.db.get_track(track.id) == track


@pytest.mark.parametrize("case", [
    "unknown", "deleted", "outside", "trash", "symlink", "symlink-parent",
    "missing", "not-audio", "directory",
])
def test_playback_rejects_unavailable_or_unsafe_sources(lib_env, case):
    env = lib_env
    path = env.library / "track.wav"
    make_wav(path, seconds=0.1)
    expected = 403
    if case == "outside":
        path = env.tmp / "outside.wav"
        make_wav(path, seconds=0.1)
    elif case == "trash":
        path = Path(env.config.trash_dir) / "track.wav"
        make_wav(path, seconds=0.1)
    elif case == "symlink":
        linked = env.library / "linked.wav"
        linked.symlink_to(path)
        path = linked
    elif case == "symlink-parent":
        linked = env.library / "linked-folder"
        linked.symlink_to(env.library, target_is_directory=True)
        path = linked / path.name
    elif case == "missing":
        path.unlink()
        expected = 404
    elif case == "not-audio":
        path = env.library / "settings.txt"
        path.write_text("not audio")
    elif case == "directory":
        path = env.library / "folder.wav"
        path.mkdir()
        expected = 404
    elif case in ("unknown", "deleted"):
        expected = 404
    track = catalog_file(env, path, status="deleted" if case == "deleted" else "ok")
    track_id = 99999 if case == "unknown" else track.id
    with TestClient(create_app(env.config)) as client:
        for suffix in ("playback", "audio", "audio?mode=compatible"):
            assert client.get(f"/api/tracks/{track_id}/{suffix}").status_code == expected


def test_playback_validates_mode_and_seek(lib_env):
    path = lib_env.library / "track.wav"
    make_wav(path, seconds=2)
    track = catalog_file(lib_env, path)
    with TestClient(create_app(lib_env.config)) as client:
        url = f"/api/tracks/{track.id}/audio"
        for params in (
            {"mode": "bad"}, {"start": -1}, {"start": "nan"}, {"start": "inf"},
        ):
            assert client.get(url, params=params).status_code == 422
        assert client.get(url, params={"start": 1}).status_code == 400
        assert client.get(url, params={"mode": "compatible", "start": 2}).status_code == 416


@pytest.mark.parametrize(("extension", "codec"), [
    ("wav", "pcm_s16le"), ("flac", "flac"), ("mp3", "libmp3lame"),
    ("m4a", "aac"), ("alac.m4a", "alac"), ("aac", "aac"),
    ("ogg", "libvorbis"), ("opus", "libopus"), ("wma", "wmav2"),
    ("aiff", "pcm_s16be"), ("wv", "wavpack"),
])
def test_compatibility_decodes_multiple_formats_without_changing_original(lib_env, extension, codec):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is needed for audio integration tests")
    env = lib_env
    source = env.tmp / "source.wav"
    make_wav(source, seconds=2)
    path = env.library / f"listen.{extension}"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-c:a", codec, str(path)],
        check=True, capture_output=True,
    )
    track = catalog_file(env, path)
    before = path.read_bytes()
    with TestClient(create_app(env.config)) as client:
        info = client.get(f"/api/tracks/{track.id}/playback").json()
        assert info["compatible_available"] is True
        response = client.get(info["compatible_url"] + "&start=1")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        decoded = env.tmp / "preview.mp3"
        decoded.write_bytes(response.content)
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(decoded), "-f", "null", "-"],
            check=False, capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        from mutagen.mp3 import MP3
        audio = MP3(decoded)
        assert 0.9 < audio.info.length < 1.3
        assert audio.info.sample_rate == 48000
        assert audio.info.channels == 2
    assert path.read_bytes() == before
    assert env.db.get_track(track.id) == track


def test_compatibility_requires_ffmpeg_but_original_still_plays(lib_env, monkeypatch):
    path = lib_env.library / "track.wav"
    make_wav(path, seconds=0.1)
    track = catalog_file(lib_env, path)
    monkeypatch.setattr(playback_module.shutil, "which", lambda _: None)
    with TestClient(create_app(lib_env.config)) as client:
        assert client.get(f"/api/tracks/{track.id}/playback").json()["compatible_available"] is False
        assert client.get(f"/api/tracks/{track.id}/audio").status_code == 200
        response = client.get(f"/api/tracks/{track.id}/audio?mode=compatible")
        assert response.status_code == 503
        assert "FFmpeg" in response.json()["detail"]


def test_corrupt_audio_reports_decode_failure_and_releases_slots(lib_env):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is needed for audio integration tests")
    path = lib_env.library / "broken.flac"
    path.write_bytes(b"not audio")
    track = catalog_file(lib_env, path, status="bad")
    with TestClient(create_app(lib_env.config)) as client:
        for _ in range(3):
            response = client.get(f"/api/tracks/{track.id}/audio?mode=compatible")
            assert response.status_code == 422
            assert "decode" in response.json()["detail"]


def test_compatibility_limits_streams_and_cleans_up_on_stop(lib_env, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is needed for audio integration tests")
    path = lib_env.library / "track.wav"
    make_wav(path, seconds=30)
    track = catalog_file(lib_env, path)
    player = Playback(lib_env.config, lib_env.db)
    processes = []
    create_process = asyncio.create_subprocess_exec

    async def record_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(playback_module.asyncio, "create_subprocess_exec", record_process)

    async def run():
        responses = [await player.compatible(track, 0) for _ in range(2)]
        try:
            with pytest.raises(HTTPException) as exc:
                await player.compatible(track, 0)
            assert exc.value.status_code == 503
            for response in responses:
                assert await response.body_iterator.__anext__()
                await response.body_iterator.aclose()
            response = await player.compatible(track, 0)
            await response.background()
        finally:
            for response in responses:
                await response.background()
        assert player.slots.acquire(blocking=False)
        assert player.slots.acquire(blocking=False)
        player.slots.release()
        player.slots.release()
        assert all(process.returncode is not None for process in processes)

    asyncio.run(run())


def test_compatibility_start_failure_releases_slots(lib_env, monkeypatch):
    path = lib_env.library / "track.wav"
    make_wav(path, seconds=0.1)
    track = catalog_file(lib_env, path)
    player = Playback(lib_env.config, lib_env.db)
    monkeypatch.setattr(playback_module.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    async def fail_start(*args, **kwargs):
        raise OSError("cannot start decoder")

    monkeypatch.setattr(playback_module.asyncio, "create_subprocess_exec", fail_start)

    async def run():
        for _ in range(3):
            with pytest.raises(HTTPException) as exc:
                await player.compatible(track, 0)
            assert exc.value.status_code == 503
            assert "start FFmpeg" in exc.value.detail

    asyncio.run(run())


def test_compatibility_send_failure_still_stops_decoder(lib_env):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is needed for audio integration tests")
    path = lib_env.library / "track.wav"
    make_wav(path, seconds=30)
    track = catalog_file(lib_env, path)
    player = Playback(lib_env.config, lib_env.db)

    async def run():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise RuntimeError("client went away")

        response = await player.compatible(track, 0)
        with pytest.raises(RuntimeError, match="client went away"):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send,
            )
        assert player.slots.acquire(blocking=False)
        assert player.slots.acquire(blocking=False)
        player.slots.release()
        player.slots.release()

    asyncio.run(run())
