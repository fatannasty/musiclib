import httpx
import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient

from musiclib import metadata_review as catalog
from musiclib.models import Track
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


def deezer_item(album=False):
    item = {
        "id": 123, "title": "Album" if album else "Song",
        "artist": {"name": "Artist"}, "link": "https://www.deezer.com/track/123",
    }
    cover = {"title": "Album", "cover_medium": "https://cdn-images.dzcdn.net/cover.jpg"}
    return {**item, **cover} if album else {**item, "album": cover}


@pytest.mark.parametrize("albums", [False, True])
def test_deezer_parses_bounded_candidates_without_inventing_unknown_metadata(monkeypatch, albums):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"data": [deezer_item(albums), None, {}]},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(catalog, "catalog_get", get)
    if albums:
        candidates = catalog.search_deezer_albums('Artist" OR', "Album", ipv4_only=True)
    else:
        candidates = catalog.search_deezer("Song", 'Artist" OR', Track(title="Song"), ipv4_only=True)
    assert len(candidates) == 1
    fields = candidates[0]["fields"]
    assert candidates[0]["source"] == "Deezer"
    assert fields["album"] == "Album"
    assert fields["year"] is None
    assert fields["mbid_release"] is None
    if not albums:
        assert fields["album_artist"] is None
        assert fields["track_no"] is None
    assert calls[0][1]["params"]["limit"] == 6
    assert calls[0][1]["ipv4_only"] is True
    assert 'artist:"Artist  OR"' in calls[0][1]["params"]["q"]


@pytest.mark.parametrize("payload", [
    {"error": {"message": "Service unavailable"}}, {"data": None}, [], {},
])
def test_deezer_bad_responses_are_explicit(monkeypatch, payload):
    monkeypatch.setattr(catalog, "catalog_get", lambda url, **kwargs: httpx.Response(
        200, json=payload, request=httpx.Request("GET", url),
    ))
    with pytest.raises(RuntimeError, match="Deezer"):
        catalog.search_deezer("Song", "Artist", Track(title="Song"))


def test_deezer_connection_failure_is_not_an_empty_success(monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("NAS offline")
    monkeypatch.setattr(catalog, "catalog_get", fail)
    with pytest.raises(RuntimeError, match="Deezer.*NAS offline"):
        catalog.search_deezer_albums("Artist", "Album")


@pytest.mark.parametrize("source", ["musicbrainz", "itunes", "deezer", "all"])
def test_track_lookup_contacts_only_selected_sources(monkeypatch, source):
    calls = []

    def provider(name):
        def search(*args, **kwargs):
            calls.append(name)
            return [{
                "source": name, "confidence": 70, "fields": {
                    "title": "Song", "artist": "Artist", "album": name,
                },
            }]
        return search
    for name in ("musicbrainz", "itunes", "deezer"):
        monkeypatch.setattr(catalog, f"search_{name}", provider(name))
    candidates = catalog.lookup_track(Track(title="Song", artist="Artist"), source=source)
    assert set(calls) == ({"musicbrainz", "itunes", "deezer"} if source == "all" else {source})
    assert len(candidates) == len(calls)


def test_album_deezer_fallback_is_read_only_and_preserves_provider_warnings(lib_env, monkeypatch):
    make_mp3(lib_env.library / "song.mp3", tags={"title": "Song", "artist": "Artist"})
    Scanner(lib_env.config, lib_env.db, workers=1).scan()
    track = lib_env.db.all_tracks()[0]

    def offline(*args, **kwargs):
        raise RuntimeError("Other catalog unavailable")
    monkeypatch.setattr(catalog, "search_musicbrainz_releases", offline)
    monkeypatch.setattr(catalog, "search_itunes_albums", offline)
    monkeypatch.setattr(catalog, "search_deezer_albums",
                        lambda *args, **kwargs: [catalog._deezer_candidate(deezer_item(True), True)])
    original_hash = track.file_hash
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/album-suggest", json={
            "track_ids": [track.id], "album": "Album", "artist": "Artist", "source": "all",
        })
        assert response.status_code == 200
        assert response.json()["suggestions"][0]["source"] == "Deezer"
        assert len(response.json()["warnings"]) == 2
        assert lib_env.db.get_track(track.id).file_hash == original_hash
        assert client.get("/api/jobs").json() == []
