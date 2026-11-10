from conftest import JPEG_600x400, make_flac, make_mp3
from fastapi.testclient import TestClient

from musiclib.scanner import Scanner
from musiclib.server.app import create_app


def test_album_page_groups_tracks_and_unknowns(lib_env):
    env = lib_env
    make_mp3(env.library / "a1.mp3", tags={
        "title": "One", "artist": "Known Artist", "album": "Night Songs",
        "albumartist": "Known Artist", "date": "2001",
    })
    make_mp3(env.library / "a2.mp3", tags={
        "title": "Two", "artist": "Known Artist", "album": "Night Songs",
        "albumartist": "Known Artist", "date": "2001",
    })
    make_mp3(env.library / "b1.mp3", tags={
        "title": "Other", "artist": "Other Artist", "album": "Other Album",
    })
    make_mp3(env.library / "loose.mp3")
    Scanner(env.config, env.db).scan()
    with TestClient(create_app(env.config)) as client:
        page = client.get("/api/albums/page").json()
        albums = {(item["album_artist"], item["album"]): item for item in page["items"]}
        assert page["total"] == 3
        assert albums[("Known Artist", "Night Songs")]["track_count"] == 2
        assert albums[("Known Artist", "Night Songs")]["year"] == 2001
        assert albums[("Other Artist", "Other Album")]["track_count"] == 1
        assert albums[("Unknown Artist", "Unknown Album")]["track_count"] == 1
        filtered = client.get("/api/albums/page", params={"query": "Night"}).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["album"] == "Night Songs"
        missing = client.get("/api/albums/detail", params={
            "album_artist": "Nobody", "album": "Missing",
        })
        assert missing.status_code == 404
        detail = client.get("/api/albums/detail", params={
            "album_artist": "Known Artist", "album": "Night Songs",
        }).json()
        assert detail["track_count"] == 2
        assert {track["title"] for track in detail["tracks"]} == {"One", "Two"}


def test_track_cover_serves_embedded_art(lib_env):
    env = lib_env
    make_flac(env.library / "cover.flac", tags={"title": "Art", "album": "Covered"}, art=JPEG_600x400)
    make_mp3(env.library / "plain.mp3", tags={"title": "Plain", "album": "Bare"})
    Scanner(env.config, env.db).scan()
    tracks = {track.title: track for track in env.db.all_tracks()}
    with TestClient(create_app(env.config)) as client:
        page = client.get("/api/albums/page").json()
        covered = next(item for item in page["items"] if item["album"] == "Covered")
        assert covered["has_art"] is True
        assert covered["cover_url"].endswith(f"/api/tracks/{tracks['Art'].id}/cover")
        response = client.get(covered["cover_url"])
        assert response.status_code == 200
        assert response.content[:3] == b"\xff\xd8\xff"
        bare = next(item for item in page["items"] if item["album"] == "Bare")
        assert bare["cover_url"] is None
        assert client.get(f"/api/tracks/{tracks['Plain'].id}/cover").status_code == 404
        assert client.get("/api/tracks/999999/cover").status_code == 404
