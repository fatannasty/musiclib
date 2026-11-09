import os

from fastapi.testclient import TestClient

from musiclib.fsops import trash_file
from musiclib.models import Track
from musiclib.server.app import create_app


def test_trash_restore_uses_existing_database_entry(lib_env):
    env = lib_env
    original = env.library / "Artist" / "song.flac"
    original.parent.mkdir()
    original.write_bytes(b"audio")
    track = env.db.upsert_track(Track(path=str(original), size=5, status="deleted"))
    trashed = trash_file(str(original), env.config.trash_dir)
    env.db.record_deletion(str(original), 5, "hash", "duplicate", trashed)
    entry = env.db.query_one("SELECT id FROM deletions")["id"]

    with TestClient(create_app(env.config)) as client:
        page = client.get("/api/trash").json()
        assert page["total"] == 1
        assert page["items"][0]["moved_to"] == trashed
        response = client.post("/api/trash/restore", json={"deletion_id": entry})
        assert response.status_code == 200
        assert response.json()["restored"] == str(original.resolve())
        assert original.read_bytes() == b"audio"
        assert not os.path.exists(trashed)
        assert env.db.get_track(track.id).status == "suspect"
        assert client.get("/api/trash").json()["total"] == 0
        assert client.get("/api/trash?include_restored=true").json()["total"] == 1


def test_restore_refuses_occupied_path_without_overwriting(lib_env):
    env = lib_env
    original = env.library / "song.flac"
    original.write_bytes(b"new")
    trashed = trash_file(str(original), env.config.trash_dir)
    original.write_bytes(b"existing")
    env.db.record_deletion(str(original), 3, None, "test", trashed)
    entry = env.db.query_one("SELECT id FROM deletions")["id"]

    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/trash/restore", json={"deletion_id": entry})
        assert response.status_code == 409
        assert original.read_bytes() == b"existing"
        assert open(trashed, "rb").read() == b"new"


def test_restore_refuses_paths_outside_trash_and_library(lib_env, tmp_path):
    env = lib_env
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"data")
    env.db.record_deletion(str(outside), 4, None, "forged", str(outside))
    entry = env.db.query_one("SELECT id FROM deletions")["id"]
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/trash/restore", json={"deletion_id": entry})
        assert response.status_code == 400
        assert outside.read_bytes() == b"data"


def test_track_browser_filters_and_pages_results(lib_env):
    env = lib_env
    for index in range(60):
        env.db.upsert_track(Track(
            path=str(env.library / f"{index:03}.flac"),
            artist="Artist", title=f"Song {index}", genre="Jazz" if index % 2 else "Rock",
            format="flac", status="suspect" if index == 0 else "ok",
        ))
    with TestClient(create_app(env.config)) as client:
        page = client.post("/api/tracks/page", json={"limit": 50, "offset": 50}).json()
        assert page["total"] == 60
        assert len(page["items"]) == 10
        filtered = client.post(
            "/api/tracks/page", json={"genre": "Rock", "format": "flac", "status": "suspect"}
        ).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["title"] == "Song 0"
        assert client.get("/api/genres").json() == ["Jazz", "Rock"]
        assert client.get("/api/formats").json() == ["flac"]
