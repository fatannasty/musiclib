from __future__ import annotations

import threading

import httpx
import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.edition_audit import compare_tracks
from musiclib.scanner import Scanner
from musiclib.server.app import create_app

RELEASE = "12345678-1234-5678-1234-567812345678"
OTHER = "87654321-4321-8765-4321-876543218765"


def _local(track_id, title, position, duration=100):
    return {
        "id": track_id, "title": title, "disc_no": 1,
        "track_no": position, "duration": duration,
    }


def _catalog(title, position, duration=100):
    return {"title": title, "disc": 1, "position": position, "duration": duration}


def test_comparison_flags_order_duration_and_unmatched_without_guessing():
    result = compare_tracks(
        [_local(1, "First", 2, 110), _local(2, "Local only", 1)],
        [_catalog("First", 1), _catalog("Catalog only", 2)],
    )
    assert result["tracks"][0]["local"]["id"] == 1
    assert result["tracks"][0]["flags"] == [
        "Different disc or track number", "Duration differs by more than 5 seconds",
    ]
    assert result["tracks"][1]["local"] is None
    assert [track["id"] for track in result["unmatched_local"]] == [2]


def test_duplicate_titles_are_not_guessed():
    result = compare_tracks(
        [_local(1, "Intro", 1), _local(2, "Intro", 2)],
        [_catalog("Intro", 1), _catalog("Intro", 2)],
    )
    assert all(item["local"] is None for item in result["tracks"])
    assert len(result["unmatched_local"]) == 2


def test_album_audit_saves_multiple_editions_without_changing_files(lib_env, monkeypatch):
    paths = []
    for index, title in enumerate(("First", "Local only"), 1):
        path = lib_env.library / f"{index}.mp3"
        make_mp3(path, tags={
            "artist": "Artist", "albumartist": "Artist", "album": "Mixtape",
            "title": title, "tracknumber": str(index),
        })
        paths.append(path)
    Scanner(lib_env.config, lib_env.db, workers=1).scan()
    before = [path.read_bytes() for path in paths]
    monkeypatch.setattr("musiclib.edition_audit.brainz.search_releases", lambda **kwargs: {
        "release-list": [
            {"id": RELEASE, "title": "Mixtape", "date": "2003", "country": "US"},
            {"id": OTHER, "title": "Mixtape deluxe"},
        ],
    })
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        if OTHER in url:
            return httpx.Response(503, request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "id": RELEASE, "media": [{"position": 1, "tracks": [
                {"position": 2, "title": "First", "length": 100000},
                {"position": 1, "title": "Catalog only"},
            ]}],
        })

    monkeypatch.setattr("musiclib.brainz.catalog_get", get)
    lib_env.config.catalog["ipv4_only"] = True
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/album-tracklist-audit", json={
            "album_artist": "Artist", "album": "Mixtape",
        })
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "done" and job["read_only"]
        assert job["summary"]["editions"] == 2
        editions = client.get(f"/api/jobs/{job_id}/results/editions?limit=6").json()["items"]
        assert editions[0]["tracks"][0]["local"]["title"] == "First"
        assert "Different disc or track number" in editions[0]["tracks"][0]["flags"]
        assert editions[0]["tracks"][1]["local"] is None
        assert editions[0]["unmatched_local"][0]["title"] == "Local only"
        assert "track-list lookup failed" in editions[1]["error"]
        assert all(options["ipv4_only"] for _, options in requests)
        assert [path.read_bytes() for path in paths] == before


def test_no_album_or_catalog_failure_is_reported(lib_env, monkeypatch):
    with TestClient(create_app(lib_env.config)) as client:
        assert client.post("/api/metadata/album-tracklist-audit", json={
            "album_artist": " ", "album": "Mixtape",
        }).status_code == 400
        job_id = client.post("/api/metadata/album-tracklist-audit", json={
            "album_artist": "Artist", "album": "Missing",
        }).json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "error"
        assert "no longer available" in job["error"]


def test_audit_runs_alongside_artwork_discovery(lib_env, monkeypatch):
    path = lib_env.library / "track.mp3"
    make_mp3(path, tags={
        "artist": "Artist", "albumartist": "Artist", "album": "Mixtape", "title": "First",
    })
    Scanner(lib_env.config, lib_env.db, workers=1).scan()
    started, finish = threading.Event(), threading.Event()

    def discover(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return {"albums": [], "candidates": 0}

    monkeypatch.setattr("musiclib.server.app.discover_artwork_batch", discover)
    monkeypatch.setattr("musiclib.edition_audit.brainz.search_releases", lambda **kwargs: {
        "release-list": [],
    })
    with TestClient(create_app(lib_env.config)) as client:
        artwork = client.post("/api/artwork/discover", json={}).json()["job_id"]
        try:
            assert started.wait(5)
            job_id = client.post("/api/metadata/album-tracklist-audit", json={
                "album_artist": "Artist", "album": "Mixtape",
            }).json()["job_id"]
            assert wait_job(client, job_id)["status"] == "done"
        finally:
            finish.set()
        assert wait_job(client, artwork)["status"] == "done"


@pytest.mark.parametrize("title", ["", " "])
def test_blank_identity_is_rejected(lib_env, title):
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/album-tracklist-audit", json={
            "album_artist": "Artist", "album": title,
        })
        assert response.status_code in (400, 422)
