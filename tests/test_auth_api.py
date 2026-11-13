from __future__ import annotations

import sqlite3

from conftest import make_wav
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from musiclib.cli import app as cli_app
from musiclib.db import Database
from musiclib.models import Track
from musiclib.server.app import create_app
from musiclib.server.auth import hash_password

ORIGIN = {"Origin": "https://testserver"}


def create_admin(env, username="admin"):
    return env.db.create_user(username, hash_password("Correct horse battery staple!"), "admin")


def login(client, username, password="Correct horse battery staple!"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=ORIGIN
    )


def csrf_headers(client):
    token = client.get("/api/auth/session").json()["csrf_token"]
    return {**ORIGIN, "X-CSRF-Token": token}


def test_authentication_requires_login_and_same_origin(lib_env):
    create_admin(lib_env)
    with TestClient(create_app(lib_env.config, require_auth=True), base_url="https://testserver") as client:
        for method, path in (
            ("get", "/api/stats"),
            ("get", "/api/settings"),
            ("get", "/api/jobs"),
            ("post", "/api/scan"),
            ("put", "/api/settings"),
            ("get", "/api/tracks/1/audio"),
            ("get", "/docs"),
            ("get", "/openapi.json"),
        ):
            denied = getattr(client, method)(path)
            assert denied.status_code == 401
            assert denied.headers["x-frame-options"] == "DENY"
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Correct horse battery staple!"},
        ).status_code == 403
        response = login(client, "admin")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
        assert client.get("/api/auth/session").json()["user"]["role"] == "admin"
        assert client.get("/api/settings").status_code == 200
        assert client.put("/api/settings", json={}).status_code == 403
        assert client.put("/api/settings", json={}, headers=csrf_headers(client)).status_code == 200
        assert client.post("/api/auth/logout", headers=csrf_headers(client)).status_code == 200
        assert client.get("/api/stats").status_code == 401


def test_users_are_scoped_and_cannot_access_admin_operations(lib_env):
    create_admin(lib_env)
    with TestClient(create_app(lib_env.config, require_auth=True), base_url="https://testserver") as admin:
        assert login(admin, "admin").status_code == 200
        created = admin.post(
            "/api/auth/users",
            json={"username": "listener", "password": "A different secure passphrase"},
            headers=csrf_headers(admin),
        )
        assert created.status_code == 200
        admin_playlist = admin.post(
            "/api/playlists", json={"name": "Admin list"}, headers=csrf_headers(admin)
        ).json()
        with TestClient(
            create_app(lib_env.config, require_auth=True), base_url="https://testserver"
        ) as listener:
            assert login(
                listener, "listener", "A different secure passphrase"
            ).status_code == 200
            assert listener.get("/api/settings").status_code == 403
            assert listener.get("/api/playlists").json() == []
            assert listener.get(f"/api/playlists/{admin_playlist['id']}").status_code == 404
            assert listener.post(
                "/api/playlists", json={"name": "Private"},
            ).status_code == 403
            own = listener.post(
                "/api/playlists", json={"name": "Private"}, headers=csrf_headers(listener)
            )
            assert own.status_code == 200
            assert [item["name"] for item in listener.get("/api/playlists").json()] == ["Private"]
            assert len(admin.get("/api/playlists").json()) == 2
            assert listener.get("/api/jobs").status_code == 403


def test_password_change_revokes_prior_sessions(lib_env):
    create_admin(lib_env)
    with TestClient(
        create_app(lib_env.config, require_auth=True), base_url="https://testserver"
    ) as first:
        assert login(first, "admin").status_code == 200
        with TestClient(
            create_app(lib_env.config, require_auth=True), base_url="https://testserver"
        ) as second:
            assert login(second, "admin").status_code == 200
            changed = first.post(
                "/api/auth/password",
                json={
                    "current_password": "Correct horse battery staple!",
                    "new_password": "An even safer replacement passphrase",
                },
                headers=csrf_headers(first),
            )
            assert changed.status_code == 200
            assert second.get("/api/stats").status_code == 401
            assert login(first, "admin").status_code == 401
            assert login(first, "admin", "An even safer replacement passphrase").status_code == 200


def test_first_admin_claims_legacy_playlists(lib_env):
    legacy_id = lib_env.db.create_playlist("Before accounts")
    admin_id = lib_env.db.create_user(
        "admin", hash_password("Correct horse battery staple!"), "admin"
    )
    assert lib_env.db.playlist(legacy_id, admin_id)["name"] == "Before accounts"
    assert lib_env.db.create_user(
        "listener", hash_password("A different secure passphrase"), "user"
    )
    assert lib_env.db.playlists(admin_id)[0]["name"] == "Before accounts"
    assert lib_env.db.playlists(2) == []


def test_legacy_playlist_schema_migrates_owner_column(lib_env):
    path = lib_env.tmp / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE playlists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO playlists VALUES (1, 'Legacy', 'now', 'now')"
        )
    db = Database(path)
    try:
        admin_id = db.create_user(
            "admin", hash_password("Correct horse battery staple!"), "admin"
        )
        assert db.playlist(1, admin_id)["name"] == "Legacy"
    finally:
        db.close()


def test_listener_can_browse_album_tracks_in_playback_order(lib_env):
    create_admin(lib_env)
    for path, disc, number in (
        ("disc2-track1.mp3", 2, 1),
        ("disc1-track2.mp3", 1, 2),
        ("disc1-track1.mp3", 1, 1),
    ):
        lib_env.db.upsert_track(Track(
            path=str(lib_env.library / path), artist="Artist", album_artist="Artist",
            album="Album", title=path, disc_no=disc, track_no=number,
        ))
    app = create_app(lib_env.config, require_auth=True)
    with TestClient(app, base_url="https://testserver") as admin:
        assert login(admin, "admin").status_code == 200
        admin.post(
            "/api/auth/users",
            json={"username": "listener", "password": "A different secure passphrase"},
            headers=csrf_headers(admin),
        )
    with TestClient(
        create_app(lib_env.config, require_auth=True), base_url="https://testserver"
    ) as listener:
        assert login(listener, "listener", "A different secure passphrase").status_code == 200
        page = listener.get("/api/listen/albums").json()
        assert page["total"] == 1
        detail = listener.get("/api/listen/album", params={
            "album_artist": "Artist", "album": "Album",
        }).json()
        assert [track["track_no"] for track in detail["tracks"]] == [1, 2, 1]
        assert [track["disc_no"] for track in detail["tracks"]] == [1, 1, 2]
        assert all("path" not in track and "file_hash" not in track for track in detail["tracks"])


def test_listener_is_denied_every_registered_administration_operation(lib_env):
    create_admin(lib_env)
    lib_env.db.create_user("listener", hash_password("A different secure passphrase"), "user")
    app = create_app(lib_env.config, require_auth=True)
    listening_routes = {
        "/api/auth/login", "/api/auth/session", "/api/auth/password", "/api/auth/logout",
        "/api/listen/tracks", "/api/listen/albums", "/api/listen/album",
        "/api/listen/favorites", "/api/favorites/{track_id}",
        "/api/tracks/{track_id}/playback", "/api/tracks/{track_id}/audio",
        "/api/tracks/{track_id}/cover",
    }
    checked = 0
    with TestClient(app, base_url="https://testserver") as client:
        assert login(client, "listener", "A different secure passphrase").status_code == 200
        headers = csrf_headers(client)
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/") or path in listening_routes or path.startswith("/api/playlists"):
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                # Authorization must reject the route before body/path validation or tool execution.
                response = client.request(method, path, headers=headers)
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["detail"] == "Administrator access required."
                checked += 1
        assert checked > 40
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 403
        assert client.get("/api/listen/tracks").status_code == 200


def test_listener_playlist_contains_music_not_server_paths(lib_env):
    create_admin(lib_env)
    user_id = lib_env.db.create_user(
        "listener", hash_password("A different secure passphrase"), "user"
    )
    path = lib_env.library / "private-folder" / "song.wav"
    path.parent.mkdir()
    make_wav(path, seconds=0.1)
    track = lib_env.db.upsert_track(Track(path=str(path), title="A song", format="wav"))
    playlist_id = lib_env.db.create_playlist("My songs", user_id)
    with TestClient(
        create_app(lib_env.config, require_auth=True), base_url="https://testserver"
    ) as client:
        login(client, "listener", "A different secure passphrase")
        response = client.post(
            f"/api/playlists/{playlist_id}/tracks",
            json={"track_ids": [track.id]}, headers=csrf_headers(client),
        )
        assert response.status_code == 200
        song = response.json()["tracks"][0]
        assert song["title"] == "A song"
        assert "path" not in song and "status" not in song
        detail = client.get(f"/api/playlists/{playlist_id}")
        assert "private-folder" not in detail.text


def test_cli_creates_only_the_first_admin_without_printing_password(lib_env):
    runner = CliRunner()
    password = "Private secure test passphrase"
    result = runner.invoke(
        cli_app,
        ["create-admin", "--config", str(lib_env.config.config_path)],
        input=f"owner\n{password}\n{password}\n",
    )
    assert result.exit_code == 0, result.output
    assert "Administrator account created" in result.output
    assert password not in result.output
    assert lib_env.db.get_user_by_username("owner")["role"] == "admin"

    second = runner.invoke(
        cli_app,
        ["create-admin", "--config", str(lib_env.config.config_path)],
    )
    assert second.exit_code == 1
    assert "bootstrap is closed" in second.output
