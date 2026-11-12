from __future__ import annotations

import io
import threading

import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient
from PIL import Image
from test_jobs_api import wait_job

from musiclib.artwork_discovery import _trusted_cover_url, download_candidate_cover
from musiclib.models import Album
from musiclib.scanner import Scanner
from musiclib.server.app import create_app
from musiclib.server.artwork_schedule import ArtworkSchedule
from musiclib.server.jobs import JobRegistry


def _image():
    output = io.BytesIO()
    with Image.new("RGB", (400, 400), "blue") as image:
        image.save(output, format="PNG")
    return output.getvalue()


def _candidate():
    return {
        "source": "iTunes", "confidence": 92,
        "cover_url": "https://is1-ssl.mzstatic.com/image/thumb/cover/300x300bb.jpg",
        "fields": {
            "album": "Album (catalog)", "album_artist": "Artist",
            "artist": "Artist", "mbid_release": None,
        },
    }


def _seed_album(env):
    paths = [
        env.library / "Artist" / f"track-{index}.mp3"
        for index in range(2)
    ]
    for index, path in enumerate(paths):
        make_mp3(path, tags={
            "artist": "Artist", "albumartist": "Artist", "album": "Album",
            "title": f"Track {index + 1}",
        })
    Scanner(env.config, env.db, workers=1).scan()
    return paths


def _mock_suggestion(env, track_ids, album=None, artist=None, source=None):
    return {
        "tracks": [
            {
                "id": track.id, "path": track.path, "file_hash": track.file_hash,
                "title": track.title, "artist": track.artist,
            }
            for track_id in track_ids
            if (track := env.db.get_track(track_id))
        ],
        "suggestions": [_candidate()],
        "warnings": [],
        "source": source,
        "query": {"album": album, "artist": artist},
    }


def test_artwork_discovery_is_read_only_and_returns_catalog_candidates(lib_env, monkeypatch):
    paths = _seed_album(lib_env)
    originals = [path.read_bytes() for path in paths]
    monkeypatch.setattr(
        "musiclib.server.app.MetadataReview.suggest_album",
        lambda self, track_ids, album=None, artist=None, source=None:
            _mock_suggestion(lib_env, track_ids, album, artist, source),
    )
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/artwork/discover", json={})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert job["kind"] == "artwork-discovery"
        assert job["read_only"]
        result = client.get(f"/api/jobs/{job_id}/results/albums?limit=25").json()
        assert result["total"] == 1
        album = result["items"][0]
        assert album["status"] == "suggestions"
        assert album["tracks"] and album["tracks"][0]["file_hash"]
        assert album["suggestions"][0]["source"] == "iTunes"
        assert [path.read_bytes() for path in paths] == originals


def test_candidate_cover_uses_snapshot_review_before_embedding(lib_env, monkeypatch):
    paths = _seed_album(lib_env)
    monkeypatch.setattr(
        "musiclib.server.app.MetadataReview.suggest_album",
        lambda self, track_ids, album=None, artist=None, source=None:
            _mock_suggestion(lib_env, track_ids, album, artist, source),
    )
    monkeypatch.setattr("musiclib.server.app.download_candidate_cover", lambda *args: _image())
    with TestClient(create_app(lib_env.config)) as client:
        discovery_id = client.post("/api/artwork/discover", json={}).json()["job_id"]
        assert wait_job(client, discovery_id)["status"] == "done"
        preview_response = client.post("/api/artwork/candidate-preview", json={
            "discovery_job_id": discovery_id, "album_index": 0, "candidate_index": 0,
        })
        assert preview_response.status_code == 200
        preview_id = preview_response.json()["job_id"]
        preview = wait_job(client, preview_id)
        assert preview["status"] == "done"
        assert client.get(f"/api/artwork/reviews/{preview['summary']['review_id']}").json() == {
            "status": "review",
        }
        apply_id = client.post("/api/artwork/apply-reviewed", json={
            "preview_job_id": preview_id,
        }).json()["job_id"]
        assert wait_job(client, apply_id)["status"] == "done"
        assert all(path.is_file() for path in paths)


def test_candidate_preview_refuses_files_changed_after_discovery(lib_env, monkeypatch):
    paths = _seed_album(lib_env)
    monkeypatch.setattr(
        "musiclib.server.app.MetadataReview.suggest_album",
        lambda self, track_ids, album=None, artist=None, source=None:
            _mock_suggestion(lib_env, track_ids, album, artist, source),
    )
    with TestClient(create_app(lib_env.config)) as client:
        discovery_id = client.post("/api/artwork/discover", json={}).json()["job_id"]
        assert wait_job(client, discovery_id)["status"] == "done"
        paths[0].write_bytes(paths[0].read_bytes() + b"changed")
        response = client.post("/api/artwork/candidate-preview", json={
            "discovery_job_id": discovery_id, "album_index": 0, "candidate_index": 0,
        })
    assert response.status_code == 409
    assert "changed since artwork discovery" in response.json()["detail"]


def test_candidate_cover_download_rejects_untrusted_hosts(lib_env):
    with pytest.raises(ValueError, match="URL is not allowed"):
        download_candidate_cover({
            "source": "Deezer", "cover_url": "https://127.0.0.1/cover.jpg",
        }, lib_env.config, lib_env.db)


def test_merged_catalog_suggestions_keep_allowed_cover_urls(lib_env, monkeypatch):
    release_id = "12345678-1234-5678-1234-567812345678"
    cover = _image()
    monkeypatch.setattr(
        "musiclib.artwork_discovery.Artwork.download_front",
        lambda self, mbid: cover if mbid == release_id else None,
    )
    assert _trusted_cover_url(
        "MusicBrainz + iTunes",
        "https://is1-ssl.mzstatic.com/image/thumb/cover/300x300bb.jpg",
    )
    assert download_candidate_cover({
        "source": "MusicBrainz + iTunes",
        "cover_url": f"https://coverartarchive.org/release/{release_id}/front-250",
        "fields": {"mbid_release": release_id},
    }, lib_env.config, lib_env.db) == cover


def test_artwork_schedule_is_opt_in_persistent_and_advances_batches(lib_env):
    for index in range(26):
        lib_env.db.upsert_album(Album(
            album_artist="Artist", album=f"Album {index:02}", art_status="missing",
        ))
    jobs = JobRegistry(lib_env.db)
    calls = []

    def discover(job, albums):
        calls.append([item["id"] for item in albums])
        return {"albums": [{"album": item["album"]} for item in albums]}

    schedule = ArtworkSchedule(lib_env.config, lib_env.db, jobs, discover)
    assert schedule.status(now=100)["next_run"] is None
    schedule.tick(now=1000)
    assert not calls
    lib_env.config.artwork["schedule_hours"] = 24
    due = schedule.status(now=100)["next_run"]
    assert due == 86500
    restarted = ArtworkSchedule(lib_env.config, lib_env.db, jobs, discover)
    assert restarted.status(now=200)["next_run"] == due
    restarted.tick(now=due)
    jobs.close()
    assert len(calls[0]) == restarted.BATCH_SIZE
    assert jobs.list()[0]["origin"] == "schedule"
    assert jobs.list()[0]["status"] == "done"
    assert restarted.status(now=due)["cursor_id"] == calls[0][-1]
    restarted.tick(now=due + 24 * 3600)
    jobs.close()
    assert len(calls[1]) == 1
    assert restarted.status(now=due + 24 * 3600)["cursor_id"] == 0
    lib_env.config.artwork["schedule_hours"] = 0


def test_artwork_job_lane_can_run_with_lookups_and_renames(lib_env):
    registry = JobRegistry(lib_env.db)
    started, finish = threading.Event(), threading.Event()

    def discover(job):
        started.set()
        assert finish.wait(5)
        return {"albums": []}

    artwork_id = registry.start(
        "artwork-discovery", discover, read_only=True, lane="artwork",
    )
    try:
        assert started.wait(5)
        lookup_id = registry.start("metadata-lookup", lambda job: {}, read_only=True)
        rename_id = registry.start("rename", lambda job: {})
    finally:
        finish.set()
    registry.close()
    assert registry.get(artwork_id)["status"] == "done"
    assert registry.get(lookup_id)["status"] == "done"
    assert registry.get(rename_id)["status"] == "done"


@pytest.mark.parametrize("hours", [-1, 1, 25])
def test_invalid_artwork_schedule_does_not_enable_automation(lib_env, hours):
    with TestClient(create_app(lib_env.config)) as client:
        response = client.put("/api/settings", json={"artwork": {"schedule_hours": hours}})
        assert response.status_code == 400
        assert client.get("/api/artwork/schedule").json()["hours"] == 0


def test_artwork_schedule_is_off_until_explicitly_enabled(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        assert client.get("/api/artwork/schedule").json()["next_run"] is None
        response = client.put("/api/settings", json={"artwork": {"schedule_hours": 24}})
        assert response.status_code == 200
        status = client.get("/api/artwork/schedule").json()
        assert status["hours"] == 24
        assert status["next_run"] > 0
        assert client.get("/api/jobs").json() == []
