from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.models import Track
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


def _seed_albums(env):
    for artist, album, titles in (
        ("Artist One", "Album One", ("One", "Two")),
        ("Artist Two", "Album Two", ("Three",)),
    ):
        for index, title in enumerate(titles, 1):
            make_mp3(env.library / artist / f"{album}-{index}.mp3", tags={
                "artist": artist, "albumartist": artist, "album": album,
                "title": title, "tracknumber": str(index),
            })
    Scanner(env.config, env.db, workers=1).scan()


def _musicbrainz_candidate(artist, album):
    return {
        "source": "MusicBrainz", "confidence": 92,
        "cover_url": None, "cover_fallback": None,
        "fields": {
            "album": f"{album} (catalog match)", "album_artist": artist,
            "artist": artist, "year": 2001, "genre": None, "mbid_release": "release-id",
        },
    }


def test_batch_discovers_selected_albums_sequentially_without_changing_files(lib_env, monkeypatch):
    _seed_albums(lib_env)
    paths = sorted(lib_env.library.rglob("*.mp3"))
    originals = {path: path.read_bytes() for path in paths}
    calls = []

    def search(artist, album, **kwargs):
        calls.append((artist, album, kwargs))
        return [_musicbrainz_candidate(artist, album)]

    monkeypatch.setattr("musiclib.metadata_review.search_musicbrainz_releases", search)
    monkeypatch.setattr("musiclib.metadata_review.search_itunes_albums", lambda *a, **k: [])
    monkeypatch.setattr("musiclib.metadata_review.search_deezer_albums", lambda *a, **k: [])
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/albums-suggest", json={
            "albums": [
                {"album_artist": "Artist One", "album": "Album One"},
                {"album_artist": "Artist Two", "album": "Album Two"},
            ],
            "source": "musicbrainz",
        })
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["status"] == "done"
        result = client.get(f"/api/jobs/{job_id}/results/albums?limit=10").json()
        assert result["total"] == 2
        first, second = result["items"]
        assert [first["status"], second["status"]] == ["suggestions", "suggestions"]
        assert first["source"] == "musicbrainz"
        catalog = next(item for item in first["suggestions"] if item["source"] == "MusicBrainz")
        assert catalog["fields"]["album"] == "Album One (catalog match)"
        assert len(first["tracks"]) == 2
        assert all(track["file_hash"] for track in first["tracks"])
        assert len(second["tracks"]) == 1
        assert calls == [
            ("Artist One", "Album One", {}),
            ("Artist Two", "Album Two", {}),
        ]
        assert {path: path.read_bytes() for path in paths} == originals


def test_batch_catalog_failure_is_reported_per_album_and_other_results_survive(
    lib_env, monkeypatch,
):
    _seed_albums(lib_env)

    def suggest(reviewer, track_ids, album=None, artist=None, source=None):
        if album == "Album One":
            raise RuntimeError("MusicBrainz album lookup failed: temporary DNS error")
        return {
            "tracks": [
                {"id": track_id, "path": str(lib_env.library / f"{track_id}.mp3"), "file_hash": "hash"}
                for track_id in track_ids
            ],
            "suggestions": [_musicbrainz_candidate(artist, album)],
            "warnings": [], "source": source, "query": {"album": album, "artist": artist},
        }

    monkeypatch.setattr("musiclib.server.app.MetadataReview.suggest_album", suggest)
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/metadata/albums-suggest", json={
            "albums": [
                {"album_artist": "Artist One", "album": "Album One"},
                {"album_artist": "Artist Two", "album": "Album Two"},
            ],
            "source": "musicbrainz",
        }).json()["job_id"]
        assert wait_job(client, job_id)["status"] == "done"
        items = client.get(f"/api/jobs/{job_id}/results/albums?limit=10").json()["items"]
        assert items[0]["status"] == "error"
        assert "temporary DNS error" in items[0]["error"]
        assert items[1]["status"] == "suggestions"
        assert items[1]["suggestions"]


@pytest.mark.parametrize("albums,message", [
    ([], "1 to 10"),
    ([{"album_artist": "Artist", "album": f"Album {index}"} for index in range(11)], "1 to 10"),
    ([{"album_artist": "Artist", "album": "Album"}] * 2, "only once"),
])
def test_batch_album_discovery_rejects_unbounded_or_duplicate_inputs(lib_env, albums, message):
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/albums-suggest", json={"albums": albums})
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_batch_album_discovery_requires_a_complete_identity(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/albums-suggest", json={
            "albums": [{"album_artist": "  ", "album": "Album"}],
        })
    assert response.status_code == 400
    assert "artist and album name" in response.json()["detail"]


def test_batch_album_discovery_limits_oversized_albums_without_provider_calls(lib_env, monkeypatch):
    for index in range(201):
        lib_env.db.upsert_track(Track(
            path=str(lib_env.library / f"oversized-{index}.mp3"),
            artist="Artist", album_artist="Artist", album="Oversized",
        ))

    def should_not_search(*args, **kwargs):
        pytest.fail("Oversized albums must be rejected before provider lookup.")

    monkeypatch.setattr("musiclib.metadata_review.search_musicbrainz_releases", should_not_search)
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/metadata/albums-suggest", json={
            "albums": [{"album_artist": "Artist", "album": "Oversized"}],
            "source": "musicbrainz",
        }).json()["job_id"]
        assert wait_job(client, job_id)["status"] == "done"
        item = client.get(f"/api/jobs/{job_id}/results/albums?limit=10").json()["items"][0]
    assert item["status"] == "error"
    assert "more than 200 tracks" in item["error"]


def test_batch_suggestions_can_only_change_tags_after_explicit_review(lib_env, monkeypatch):
    _seed_albums(lib_env)
    paths = sorted(lib_env.library.rglob("*.mp3"))
    originals = {path: path.read_bytes() for path in paths}
    monkeypatch.setattr(
        "musiclib.metadata_review.search_musicbrainz_releases",
        lambda artist, album, **kwargs: [_musicbrainz_candidate(artist, album)],
    )
    monkeypatch.setattr("musiclib.metadata_review.search_itunes_albums", lambda *a, **k: [])
    monkeypatch.setattr("musiclib.metadata_review.search_deezer_albums", lambda *a, **k: [])
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/metadata/albums-suggest", json={
            "albums": [{"album_artist": "Artist One", "album": "Album One"}],
            "source": "musicbrainz",
        }).json()["job_id"]
        assert wait_job(client, job_id)["status"] == "done"
        album_result = client.get(f"/api/jobs/{job_id}/results/albums?limit=10").json()["items"][0]
        assert {path: path.read_bytes() for path in paths} == originals
        suggestion = next(
            candidate for candidate in album_result["suggestions"]
            if candidate["source"] == "MusicBrainz"
        )
        tracks = album_result["tracks"]
        response = client.post("/api/metadata/album-apply", json={
            "track_ids": [track["id"] for track in tracks],
            "fields": ["year"], "values": {"year": suggestion["fields"]["year"]},
            "snapshots": {track["id"]: {"path": track["path"], "file_hash": track["file_hash"]}
                          for track in tracks},
        })
        assert response.status_code == 200
        assert wait_job(client, response.json()["job_id"])["status"] == "done"
        assert all(lib_env.db.get_track(track["id"]).year == 2001 for track in tracks)
        assert all(path.read_bytes() != originals[path] for path in paths if path in
                   {Path(track["path"]) for track in tracks})


def test_catalog_connection_check_reports_each_provider_without_modifying_music(lib_env, monkeypatch):
    paths = []
    _seed_albums(lib_env)
    paths.extend(lib_env.library.rglob("*.mp3"))
    originals = {path: path.read_bytes() for path in paths}
    calls = []

    def check(ipv4_only):
        calls.append(ipv4_only)
        return [
            {"provider": "MusicBrainz", "status": "connected", "message": "Service responded successfully.", "duration_ms": 10},
            {"provider": "iTunes", "status": "error", "message": "Temporary failure in name resolution", "duration_ms": 4},
            {"provider": "Deezer", "status": "connected", "message": "Service responded successfully.", "duration_ms": 8},
        ]

    lib_env.config.catalog["ipv4_only"] = True
    monkeypatch.setattr("musiclib.server.app.check_catalog_connections", check)
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/catalogs/check", json={})
    assert response.status_code == 200
    assert [item["status"] for item in response.json()["providers"]] == [
        "connected", "error", "connected",
    ]
    assert calls == [True]
    assert {path: path.read_bytes() for path in paths} == originals


def test_catalog_connection_check_preserves_dns_errors_and_tests_all_sources(monkeypatch):
    calls = []

    def musicbrainz(artist, album, ipv4_only):
        calls.append(("MusicBrainz", artist, album, ipv4_only))
        raise RuntimeError("MusicBrainz album lookup failed: Temporary failure in name resolution")

    def successful(name):
        def check(artist, album, ipv4_only):
            calls.append((name, artist, album, ipv4_only))
            return []
        return check

    monkeypatch.setattr("musiclib.metadata_review.search_musicbrainz_releases", musicbrainz)
    monkeypatch.setattr(
        "musiclib.metadata_review.search_itunes_albums", successful("iTunes"),
    )
    monkeypatch.setattr(
        "musiclib.metadata_review.search_deezer_albums", successful("Deezer"),
    )
    from musiclib.metadata_review import check_catalog_connections

    providers = check_catalog_connections(ipv4_only=True)
    assert [item["status"] for item in providers] == ["error", "connected", "connected"]
    assert "Temporary failure in name resolution" in providers[0]["message"]
    assert {item[0] for item in calls} == {"MusicBrainz", "iTunes", "Deezer"}
    assert all(item[-1] is True for item in calls)


def test_catalog_diagnostics_share_lookup_lock_and_do_not_block_library_edits(
    lib_env, monkeypatch,
):
    _seed_albums(lib_env)
    entered, finish = threading.Event(), threading.Event()

    def check(ipv4_only):
        entered.set()
        assert finish.wait(10)
        return []

    monkeypatch.setattr("musiclib.server.app.check_catalog_connections", check)
    with TestClient(create_app(lib_env.config)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/catalogs/check", json={})
        try:
            assert entered.wait(10)
            assert client.post("/api/catalogs/check", json={}).status_code == 409
            assert client.post("/api/metadata/lookup", json={"track_id": 1}).status_code == 409
        finally:
            finish.set()
        assert future.result(timeout=10).status_code == 200
