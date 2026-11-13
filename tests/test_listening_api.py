import pytest
from conftest import make_wav
from fastapi.testclient import TestClient

from musiclib.models import Track
from musiclib.server.app import create_app
from musiclib.server.auth import hash_password

ORIGIN = {"Origin": "https://testserver"}


def add_track(env, title, artist="The Artist", album="An Album", status="ok", **kwargs):
    return env.db.upsert_track(Track(
        path=str(env.library / "private-directory" / f"{title}-{status}.flac"),
        title=title, artist=artist, album=album, status=status, **kwargs,
    ))


def test_favorites_are_private_and_appear_in_listener_music_views(lib_env):
    lib_env.db.create_user("admin", hash_password("Correct horse battery staple!"), "admin")
    user_id = lib_env.db.create_user(
        "listener", hash_password("A different secure passphrase"), "user"
    )
    second_user_id = lib_env.db.create_user(
        "another", hash_password("Another secure passphrase"), "user"
    )
    path = make_wav(lib_env.library / "song.wav", seconds=1)
    track = lib_env.db.upsert_track(Track(
        path=path, title="Saved song", artist="The Artist", album="An Album",
        album_artist="The Artist", format="wav",
    ))
    app = create_app(lib_env.config, require_auth=True)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.post("/api/auth/login", json={
            "username": "listener", "password": "A different secure passphrase",
        }, headers=ORIGIN).status_code == 200
        csrf = client.get("/api/auth/session").json()["csrf_token"]
        headers = {**ORIGIN, "X-CSRF-Token": csrf}
        assert client.get(f"/api/favorites/{track.id}").json() == {"favorited": False}
        assert client.put(
            f"/api/favorites/{track.id}", headers=headers
        ).json() == {"favorited": True}
        assert client.put(
            f"/api/favorites/{track.id}", headers=headers
        ).json() == {"favorited": True}
        assert client.get("/api/listen/tracks").json()["items"][0]["is_favorite"] is True
        assert client.get("/api/listen/album", params={
            "album_artist": "The Artist", "album": "An Album",
        }).json()["tracks"][0]["is_favorite"] is True
        assert client.get(f"/api/tracks/{track.id}/playback").json()["is_favorite"] is True
        favorites = client.get("/api/listen/favorites").json()
        assert favorites["total"] == 1
        assert favorites["items"][0]["id"] == track.id
        assert client.get(
            "/api/listen/favorites", params={"sort": "recent"}
        ).json()["items"][0]["id"] == track.id
        assert client.delete(
            f"/api/favorites/{track.id}", headers=headers
        ).json() == {"favorited": False}
        assert client.get("/api/listen/favorites").json()["total"] == 0
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.post("/api/auth/login", json={
            "username": "another", "password": "Another secure passphrase",
        }, headers=ORIGIN).status_code == 200
        assert client.get(f"/api/favorites/{track.id}").json() == {"favorited": False}
        assert client.get("/api/listen/favorites").json()["total"] == 0
    assert not lib_env.db.is_favorite(second_user_id, track.id)
    assert not lib_env.db.is_favorite(user_id, track.id)


def test_music_search_combines_terms_and_does_not_search_server_paths(lib_env):
    expected = add_track(lib_env, "Morning Light", album_artist="Compilation Artist", duration=123)
    add_track(lib_env, "Another Song")
    add_track(lib_env, "Morning Broken", status="bad")
    add_track(lib_env, "Morning Deleted", status="deleted")
    with TestClient(create_app(lib_env.config)) as client:
        for query in ("morning artist", "compilation light", "  morning  light "):
            page = client.get("/api/listen/tracks", params={"query": query}).json()
            assert page["total"] == 1
            assert page["items"][0]["id"] == expected.id
            assert page["items"][0]["duration"] == 123
            assert "path" not in page["items"][0]
        assert client.get("/api/listen/tracks").json()["total"] == 2
        assert client.get(
            "/api/listen/tracks", params={"query": "private-directory"}
        ).json()["total"] == 0
        albums = client.get("/api/listen/albums", params={"query": "morning artist"}).json()
        assert albums["total"] == 1
        assert albums["items"][0]["track_count"] == 1


@pytest.mark.parametrize("query", ["%", "_", "\\"])
def test_music_search_treats_wildcards_as_literal_characters(lib_env, query):
    expected = add_track(lib_env, f"Song {query} special")
    add_track(lib_env, "Other song")
    with TestClient(create_app(lib_env.config)) as client:
        for endpoint in ("/api/listen/tracks", "/api/listen/albums"):
            page = client.get(endpoint, params={"query": query}).json()
            assert page["total"] == 1
            if endpoint.endswith("tracks"):
                assert page["items"][0]["id"] == expected.id
            else:
                assert page["items"][0]["track_count"] == 1


def test_music_pages_have_stable_sorting_and_bounded_results(lib_env):
    tracks = [add_track(lib_env, f"Song {i:02}", album=f"Album {i:02}") for i in range(28)]
    with TestClient(create_app(lib_env.config)) as client:
        for endpoint in ("/api/listen/tracks", "/api/listen/albums"):
            first = client.get(endpoint, params={"sort": "title", "limit": 25}).json()
            second = client.get(endpoint, params={"sort": "title", "limit": 25, "offset": 25}).json()
            assert first["total"] == second["total"] == 28
            assert len(first["items"]) == 25
            assert len(second["items"]) == 3
            assert second["offset"] == 25
            for params in ({"limit": 101}, {"offset": -1}, {"sort": "invalid"}, {"query": "a" * 201}):
                assert client.get(endpoint, params=params).status_code == 422
        recent = client.get("/api/listen/tracks", params={"sort": "recent", "limit": 1}).json()
        assert recent["items"][0]["id"] == tracks[-1].id


def test_listening_album_excludes_damaged_files_and_returns_only_public_metadata(lib_env):
    add_track(lib_env, "Second", disc_no=1, track_no=2, duration=140)
    add_track(lib_env, "First", disc_no=1, track_no=1, duration=120)
    add_track(lib_env, "Broken", status="bad", disc_no=1, track_no=3)
    with TestClient(create_app(lib_env.config)) as client:
        detail = client.get("/api/listen/album", params={
            "album_artist": "The Artist", "album": "An Album",
        }).json()
        assert [track["title"] for track in detail["tracks"]] == ["First", "Second"]
        assert "private-directory" not in str(detail)
        assert detail["tracks"][0]["duration"] == 120
        assert client.get("/api/listen/album", params={
            "album_artist": "Missing", "album": "Missing",
        }).status_code == 404
