from conftest import make_mp3, make_wav
from fastapi.testclient import TestClient

import musiclib.server.playback as playback_module
from musiclib.models import Track, audio_format
from musiclib.server.app import create_app


def catalog_file(env, path, status="ok"):
    return env.db.upsert_track(Track(
        path=str(path), format=audio_format(str(path)), duration=2,
        status=status, title=path.stem, artist="Artist",
    ))


def test_playlist_crud_add_order_and_remove(lib_env):
    env = lib_env
    first_path = env.library / "first.mp3"
    second_path = env.library / "second.wav"
    make_mp3(first_path)
    make_wav(second_path, seconds=0.1)
    first = catalog_file(env, first_path)
    second = catalog_file(env, second_path)

    with TestClient(create_app(env.config)) as client:
        created = client.post("/api/playlists", json={"name": "  Road trip  "})
        assert created.status_code == 200
        playlist = created.json()
        assert playlist["name"] == "Road trip"
        playlist_id = playlist["id"]

        added = client.post(
            f"/api/playlists/{playlist_id}/tracks",
            json={"track_ids": [first.id, second.id]},
        )
        assert [track["id"] for track in added.json()["tracks"]] == [first.id, second.id]
        entries = added.json()["tracks"]

        repeated = client.post(
            f"/api/playlists/{playlist_id}/tracks", json={"track_ids": [first.id]},
        ).json()
        assert [track["id"] for track in repeated["tracks"]] == [
            first.id, second.id, first.id,
        ]
        entries = repeated["tracks"]

        reordered = client.put(
            f"/api/playlists/{playlist_id}/tracks/order",
            json={"entry_ids": [entries[1]["entry_id"], entries[0]["entry_id"], entries[2]["entry_id"]]},
        ).json()
        assert [track["id"] for track in reordered["tracks"]] == [
            second.id, first.id, first.id,
        ]

        removed = client.delete(
            f"/api/playlists/{playlist_id}/tracks/{entries[1]['entry_id']}",
        ).json()
        assert [track["id"] for track in removed["tracks"]] == [first.id, first.id]
        assert client.patch(
            f"/api/playlists/{playlist_id}", json={"name": "Commute"},
        ).json()["name"] == "Commute"
        assert client.get("/api/playlists").json()[0]["track_count"] == 2
        assert client.delete(f"/api/playlists/{playlist_id}").json() == {"deleted": True}
        assert client.get(f"/api/playlists/{playlist_id}").status_code == 404


def test_playlist_rejects_invalid_tracks_and_incomplete_reorder(lib_env):
    env = lib_env
    valid_path = env.library / "valid.wav"
    unsupported_path = env.library / "notes.txt"
    outside_path = env.tmp / "outside.wav"
    make_wav(valid_path, seconds=0.1)
    unsupported_path.write_text("not an audio file")
    make_wav(outside_path, seconds=0.1)
    valid = catalog_file(env, valid_path)
    unsupported = catalog_file(env, unsupported_path)
    outside = catalog_file(env, outside_path)

    with TestClient(create_app(env.config)) as client:
        playlist_id = client.post("/api/playlists", json={"name": "Test"}).json()["id"]
        for track_id, status_code in (
            (99999, 404), (outside.id, 403), (unsupported.id, 403),
        ):
            assert client.post(
                f"/api/playlists/{playlist_id}/tracks", json={"track_ids": [track_id]},
            ).status_code == status_code
        assert client.post(
            f"/api/playlists/{playlist_id}/tracks", json={"track_ids": [valid.id, valid.id]},
        ).status_code == 422
        assert client.get(f"/api/playlists/{playlist_id}").json()["tracks"] == []
        assert client.put(
            f"/api/playlists/{playlist_id}/tracks/order", json={"entry_ids": [12]},
        ).status_code == 400
        assert client.post("/api/playlists", json={"name": "  "}).status_code == 422
        assert client.delete(f"/api/playlists/{playlist_id}/tracks/12").status_code == 404


def test_playlist_survives_app_restart(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        playlist = client.post("/api/playlists", json={"name": "Saved for later"}).json()

    with TestClient(create_app(lib_env.config)) as client:
        saved = client.get(f"/api/playlists/{playlist['id']}")

    assert saved.status_code == 200
    assert saved.json()["name"] == "Saved for later"


def test_playback_format_inventory_reports_catalog_and_transcoding(lib_env, monkeypatch):
    first = lib_env.library / "track.mp3"
    second = lib_env.library / "track.wav"
    make_mp3(first)
    make_wav(second, seconds=0.1)
    catalog_file(lib_env, first)
    catalog_file(lib_env, second)
    monkeypatch.setattr(playback_module.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    with TestClient(create_app(lib_env.config)) as client:
        inventory = client.get("/api/playback/formats").json()

    assert inventory["ffmpeg_available"] is True
    assert inventory["formats"] == [
        {"format": "mp3", "tracks": 1, "supported": True, "compatibility_playback": True},
        {"format": "wav", "tracks": 1, "supported": True, "compatibility_playback": True},
    ]


def test_playback_format_inventory_reports_missing_ffmpeg(lib_env, monkeypatch):
    path = lib_env.library / "track.flac"
    path.write_bytes(b"fLaC")
    catalog_file(lib_env, path)
    monkeypatch.setattr(playback_module.shutil, "which", lambda _: None)

    with TestClient(create_app(lib_env.config)) as client:
        inventory = client.get("/api/playback/formats").json()

    assert inventory["ffmpeg_available"] is False
    assert inventory["formats"][0]["compatibility_playback"] is False
