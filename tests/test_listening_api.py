import pytest
from fastapi.testclient import TestClient

from musiclib.models import Track
from musiclib.server.app import create_app


def add_track(env, title, artist="The Artist", album="An Album", status="ok", **kwargs):
    return env.db.upsert_track(Track(
        path=str(env.library / "private-directory" / f"{title}-{status}.flac"),
        title=title, artist=artist, album=album, status=status, **kwargs,
    ))


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
