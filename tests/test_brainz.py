import json

import httpx
import musicbrainzngs
import pytest
from conftest import ALBUM_A, make_flac

import musiclib.brainz as brainz_module
from musiclib.brainz import Brainz, ExpectedTrack
from musiclib.scanner import Scanner


def _seed_album(env, tracks):
    for i, title in enumerate(tracks, start=1):
        make_flac(
            env.library / "Artist One" / "Album A" / f"t{i}.flac",
            tags={**ALBUM_A, "TITLE": title, "TRACKNUMBER": str(i)},
        )
    Scanner(env.config, env.db).scan()


def test_compare_detects_missing_and_extra(lib_env):
    env = lib_env
    _seed_album(env, ["Alpha", "Beta"])
    brainz = Brainz(env.config, env.db)
    album = env.db.albums_all()[0]
    expected = [
        ExpectedTrack(1, 1, "Alpha"),
        ExpectedTrack(1, 2, "Beta"),
        ExpectedTrack(1, 3, "Gamma"),
    ]
    missing, extra = brainz._compare(album, expected)
    assert [t.title for t in missing] == ["Gamma"]
    assert extra == []


def test_refresh_albums_with_mocked_musicbrainz(lib_env, monkeypatch):
    env = lib_env
    _seed_album(env, ["Alpha", "Beta"])

    def fake_search_releases(artist, release, limit=10):
        return {"release-list": [{"id": "rel-1", "ext:score": "100",
                                  "status": "Official", "date": "2001-04-01"}]}

    def fake_get_release_by_id(mbid, includes=None):
        return {"release": {"medium-list": [{"position": "1", "track-list": [
            {"position": "1", "title": "Alpha", "recording": {"id": "r1"}},
            {"position": "2", "title": "Beta", "recording": {"id": "r2"}},
            {"position": "3", "title": "Gamma", "recording": {"id": "r3"}},
        ]}]}}

    monkeypatch.setattr("musiclib.brainz.musicbrainzngs.search_releases",
                        fake_search_releases)
    monkeypatch.setattr("musiclib.brainz.musicbrainzngs.get_release_by_id",
                        fake_get_release_by_id)
    brainz = Brainz(env.config, env.db)
    progress = []
    gaps = brainz.refresh_albums(
        progress=lambda done, total, current: progress.append((done, total, current))
    )
    assert progress == [(1, 1, "Artist One — Album A")]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.album.mbid_release == "rel-1"
    assert gap.album.expected_tracks == 3
    assert [t.title for t in gap.missing] == ["Gamma"]
    stored = env.db.albums_all()[0]
    assert json.loads(stored.missing_tracks) == [
        {"disc": 1, "position": 3, "title": "Gamma"}
    ]
    again = brainz.albums_with_gaps()
    assert len(again) == 1
    assert [t.title for t in again[0].missing] == ["Gamma"]


@pytest.mark.parametrize("entity", ["recording", "release"])
def test_interactive_catalog_transport_preserves_search_and_xml_results(lib_env, monkeypatch, entity):
    calls = []
    xml = (
        '<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#" '
        'xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">'
        f'<{entity}-list count="1"><{entity} id="id-1" ext:score="90">'
        f'<title>Song</title></{entity}></{entity}-list></metadata>'
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, content=xml, request=httpx.Request("GET", url))

    monkeypatch.setattr(brainz_module, "_last_request", None)
    monkeypatch.setattr(brainz_module.httpx, "get", get)
    brainz_module.configure_musicbrainz(lib_env.config)
    search = brainz_module.search_recordings if entity == "recording" else brainz_module.search_releases
    result = search(artist='A "B" AND C', **{entity: "Song: (Live)\\Remix"})
    assert result[f"{entity}-list"][0]["id"] == "id-1"
    assert result[f"{entity}-list"][0]["ext:score"] == "90"
    assert len(calls) == 1
    url, options = calls[0]
    assert url == f"https://musicbrainz.org/ws/2/{entity}/"
    assert options["timeout"] == 4.0
    assert options["params"] == {
        "query": rf'artist:(a \"b\" and c) {entity}:(song\: \(live\)\\remix)',
        "limit": 6, "fmt": "xml",
    }
    assert "musiclib/0.1" in options["headers"]["User-Agent"]
    assert lib_env.config.brainz["contact"] in options["headers"]["User-Agent"]


@pytest.mark.parametrize("failure", ["timeout", "503", "invalid-xml", "invalid-response"])
def test_interactive_catalog_failure_is_not_retried(monkeypatch, failure):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if failure == "timeout":
            raise httpx.ReadTimeout("too slow")
        status = 503 if failure == "503" else 200
        content = b"not xml" if failure == "invalid-xml" else b"<metadata/>"
        return httpx.Response(status, content=content, request=httpx.Request("GET", url))

    monkeypatch.setattr(brainz_module, "_last_request", None)
    monkeypatch.setattr(brainz_module.httpx, "get", get)
    with pytest.raises(musicbrainzngs.MusicBrainzError):
        brainz_module.search_recordings(recording="Song")
    assert len(calls) == 1


def test_interactive_catalog_wait_for_busy_musicbrainz_is_bounded(monkeypatch):
    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == 4.0
            return False

    def no_network(*args, **kwargs):
        pytest.fail("A busy MusicBrainz lane must not make a network request")

    monkeypatch.setattr(brainz_module, "_request_lock", BusyLock())
    monkeypatch.setattr(brainz_module.httpx, "get", no_network)
    with pytest.raises(musicbrainzngs.NetworkError, match="MusicBrainz is busy"):
        brainz_module.search_releases(release="Album")


def test_musicbrainz_rate_limit_is_shared_between_legacy_and_interactive_requests(monkeypatch):
    clock = iter([10.0, 10.25, 11.0])
    sleeps = []
    monkeypatch.setattr(brainz_module, "_last_request", None)
    monkeypatch.setattr(brainz_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(brainz_module.time, "sleep", sleeps.append)
    with brainz_module.musicbrainz_request():
        pass
    with brainz_module.musicbrainz_request(timeout=4.0):
        pass
    assert sleeps == [0.75]
