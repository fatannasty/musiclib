import json
import threading

import httpx
import musicbrainzngs
import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient

import musiclib.audio_identify as identify_module
import musiclib.brainz as brainz_module
from musiclib.audio_identify import identify_audio
from musiclib.scanner import Scanner, file_hash
from musiclib.server.app import create_app

RECORDING = "12345678-1234-4234-8234-123456789abc"
OTHER = "87654321-4321-4321-8321-cba987654321"


def provider_response(payload):
    return httpx.Response(200, content=json.dumps(payload).encode(),
                          request=httpx.Request("POST", identify_module.ACOUSTID_LOOKUP))


@pytest.fixture
def providers(monkeypatch):
    observed = {"fingerprints": [], "requests": [], "recordings": []}

    def fingerprint(path):
        observed["fingerprints"].append(path)
        return {"fingerprint": "AQAA_encoded-fingerprint", "duration": 245}

    def post(url, **kwargs):
        observed["requests"].append((url, kwargs))
        return httpx.Response(200, json={"status": "ok", "results": [{
            "score": .96, "recordings": [{"id": RECORDING}],
        }]}, request=httpx.Request("POST", url))

    def recording(recording_id, **kwargs):
        observed["recordings"].append((recording_id, kwargs))
        return {
            "id": recording_id, "title": "Identified Song",
            "artist-credit": [
                {"name": "First Artist", "joinphrase": " feat. "},
                {"artist": {"name": "Second Artist"}},
            ],
        }

    monkeypatch.setattr(identify_module, "compute_encoded_fingerprint", fingerprint)
    monkeypatch.setattr(identify_module, "catalog_post", post)
    monkeypatch.setattr(identify_module, "get_recording", recording)
    return observed


def test_audio_lookup_posts_only_key_fingerprint_and_duration_and_does_not_guess_release(providers):
    warnings = []
    candidates = identify_audio("/private/library/unknown.mp3", "test-key",
                                ipv4_only=True, warnings=warnings)
    url, options = providers["requests"][0]
    assert url == "https://api.acoustid.org/v2/lookup"
    assert options == {
        "data": {"client": "test-key", "format": "json", "meta": "recordingids",
                 "duration": "245", "fingerprint": "AQAA_encoded-fingerprint"},
        "headers": {"User-Agent": "musiclib/0.1"}, "timeout": 10.0, "ipv4_only": True,
    }
    assert "/private" not in str(providers["requests"])
    assert providers["recordings"] == [(RECORDING, {"ipv4_only": True})]
    assert warnings == []
    assert candidates[0]["confidence"] == 96
    assert candidates[0]["fields"] == {
        "title": "Identified Song", "artist": "First Artist feat. Second Artist",
        "mbid_track": RECORDING,
    }
    assert candidates[0]["release_mbid"] is None


def test_no_key_does_not_fingerprint_or_contact_providers(providers):
    with pytest.raises(ValueError, match="application API key"):
        identify_audio("/music/file", "", warnings=[])
    assert not any(providers.values())


@pytest.mark.parametrize("payload,message", [
    (None, "unexpected response"),
    ({"status": "ok", "results": {}}, "unexpected response"),
    ({"status": "error", "error": {"message": "test-key"}}, "rejected"),
    ({"status": "ok", "results": [{"score": True}]}, "invalid match score"),
    ({"status": "ok", "results": [{"score": float("nan")}]}, "invalid match score"),
    ({"status": "ok", "results": [{"score": .8, "recordings": [{"id": "../escape"}]}]},
     "invalid recording ID"),
])
def test_invalid_acoustid_payloads_are_explicit_and_never_resolved(providers, monkeypatch, payload, message):
    monkeypatch.setattr(identify_module, "catalog_post", lambda *args, **kwargs:
                        provider_response(payload))
    with pytest.raises(RuntimeError, match=message) as error:
        identify_audio("/music/file", "test-key", warnings=[])
    assert "test-key" not in str(error.value)
    assert providers["recordings"] == []


@pytest.mark.parametrize("status", [401, 429, 503])
def test_provider_http_errors_are_not_empty_successes(providers, monkeypatch, status):
    monkeypatch.setattr(identify_module, "catalog_post", lambda url, **kwargs:
                        httpx.Response(status, request=httpx.Request("POST", url)))
    with pytest.raises(RuntimeError, match="AcoustID lookup failed"):
        identify_audio("/music/file", "test-key", warnings=[])
    assert providers["recordings"] == []


@pytest.mark.parametrize("linked", [False, True])
def test_no_matches_or_unlinked_matches_preserve_explicit_outcome(providers, monkeypatch, linked):
    results = [{"score": .99}] if linked else []
    monkeypatch.setattr(identify_module, "catalog_post", lambda *args, **kwargs:
                        provider_response({"status": "ok", "results": results}))
    warnings = []
    assert identify_audio("/music/file", "test-key", warnings=warnings) == []
    assert bool(warnings) == linked
    assert providers["recordings"] == []


def test_recording_candidates_are_deduplicated_ranked_and_bounded(providers, monkeypatch):
    ids = [f"12345678-1234-4234-8234-{number:012d}" for number in range(10)]
    results = [{"score": number / 10, "recordings": [{"id": item}]} for number, item in enumerate(ids)]
    results.append({"score": .99, "recordings": [{"id": ids[-1]}]})
    monkeypatch.setattr(identify_module, "catalog_post", lambda *args, **kwargs:
                        provider_response({"status": "ok", "results": results}))
    candidates = identify_audio("/music/file", "test-key", warnings=[])
    assert len(candidates) == 5
    assert [item[0] for item in providers["recordings"]] == list(reversed(ids))[:5]
    assert candidates[0]["confidence"] == 99


def test_partial_musicbrainz_failure_keeps_available_suggestions_with_warning(providers, monkeypatch):
    monkeypatch.setattr(identify_module, "catalog_post", lambda *args, **kwargs:
                        provider_response({"status": "ok", "results": [{
                            "score": .9, "recordings": [{"id": RECORDING}, {"id": OTHER}],
                        }]}))
    lookup = identify_module.get_recording

    def recording(recording_id, **kwargs):
        if recording_id == OTHER:
            raise RuntimeError("MusicBrainz recording lookup failed: timeout")
        return lookup(recording_id, **kwargs)

    monkeypatch.setattr(identify_module, "get_recording", recording)
    warnings = []
    assert len(identify_audio("/music/file", "test-key", warnings=warnings)) == 1
    assert warnings == ["MusicBrainz recording lookup failed: timeout"]
    def unavailable(*args, **kwargs):
        raise RuntimeError("Unavailable")

    monkeypatch.setattr(identify_module, "get_recording", unavailable)
    with pytest.raises(RuntimeError, match="Unavailable"):
        identify_audio("/music/file", "test-key", warnings=[])


def test_musicbrainz_resolution_uses_rate_limited_ipv4_aware_transport(lib_env, monkeypatch):
    calls = []
    entered = []

    class Lease:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *args):
            pass

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={
            "id": RECORDING, "title": "Resolved Song", "artist-credit": [],
        }, request=httpx.Request("GET", url))

    monkeypatch.setattr(brainz_module, "musicbrainz_request", lambda timeout: Lease())
    monkeypatch.setattr(brainz_module, "catalog_get", get)
    brainz_module.configure_musicbrainz(lib_env.config)
    assert brainz_module.get_recording(RECORDING, ipv4_only=True)["title"] == "Resolved Song"
    assert entered == [True]
    assert calls[0][0] == f"https://musicbrainz.org/ws/2/recording/{RECORDING}"
    assert calls[0][1]["params"] == {"inc": "artists", "fmt": "json"}
    assert calls[0][1]["ipv4_only"] is True
    assert calls[0][1]["timeout"] == 4.0
    monkeypatch.setattr(brainz_module, "catalog_get", lambda *args, **kwargs:
                        provider_response({"id": OTHER, "title": "Wrong", "artist-credit": []}))
    with pytest.raises(RuntimeError, match="unexpected recording"):
        brainz_module.get_recording(RECORDING)


def test_busy_musicbrainz_request_is_an_actionable_provider_error(monkeypatch):
    def busy(**kwargs):
        raise musicbrainzngs.NetworkError("MusicBrainz is busy. Please retry the search.")

    monkeypatch.setattr(brainz_module, "musicbrainz_request", busy)
    with pytest.raises(RuntimeError, match="MusicBrainz is busy"):
        brainz_module.get_recording(RECORDING)


def test_audio_identification_api_is_review_only_and_applies_only_selected_fields(lib_env, providers):
    path = lib_env.library / "unknown.mp3"
    make_mp3(path)
    Scanner(lib_env.config, lib_env.db).scan()
    track = lib_env.db.all_tracks()[0]
    before = path.read_bytes()
    lib_env.config.acoustid["api_key"] = "test-key"
    with TestClient(create_app(lib_env.config)) as client:
        review_response = client.post("/api/metadata/identify", json={"track_id": track.id})
        assert review_response.status_code == 200
        review = review_response.json()
        assert path.read_bytes() == before
        assert lib_env.db.get_track(track.id).mbid_track is None
        assert lib_env.db.get_track(track.id).fingerprint is None
        assert client.post("/api/metadata/apply", json={
            "review_id": review["review_id"], "candidate_index": 0, "fields": [],
        }).status_code == 400
        assert client.post("/api/metadata/apply", json={
            "review_id": review["review_id"], "candidate_index": 0,
            "fields": ["title", "mbid_track"],
        }).status_code == 200
        updated = lib_env.db.get_track(track.id)
        assert updated.title == "Identified Song"
        assert updated.mbid_track == RECORDING
        assert updated.artist == track.artist
        assert updated.album == track.album


@pytest.mark.parametrize("failure", ["missing-key", "missing-file", "unhashed", "stale", "outside", "symlink", "deleted"])
def test_audio_identification_rejects_unavailable_or_unsafe_inputs(lib_env, providers, failure):
    path = lib_env.library / "song.mp3"
    make_mp3(path)
    Scanner(lib_env.config, lib_env.db).scan()
    track = lib_env.db.all_tracks()[0]
    lib_env.config.acoustid["api_key"] = "" if failure == "missing-key" else "test-key"
    if failure == "missing-file":
        path.unlink()
    elif failure == "unhashed":
        lib_env.db.update_track_columns(track.id, file_hash=None)
    elif failure == "stale":
        with path.open("ab") as target:
            target.write(b"changed")
    elif failure == "outside":
        outside = lib_env.tmp / "outside.mp3"
        outside.write_bytes(path.read_bytes())
        lib_env.db.update_track_columns(track.id, path=str(outside))
    elif failure == "symlink":
        alias = lib_env.library / "alias.mp3"
        alias.symlink_to(path)
        lib_env.db.update_track_columns(track.id, path=str(alias))
    elif failure == "deleted":
        lib_env.db.update_track_columns(track.id, status="deleted")
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/identify", json={"track_id": track.id})
        assert response.status_code == 400
    assert providers["fingerprints"] == []
    assert lib_env.db.query_one("SELECT COUNT(*) AS n FROM metadata_reviews")["n"] == 0


@pytest.mark.parametrize("change", ["disk", "catalog"])
def test_file_changes_during_identification_invalidate_review(lib_env, providers, monkeypatch, change):
    path = lib_env.library / "song.mp3"
    make_mp3(path)
    Scanner(lib_env.config, lib_env.db).scan()
    track = lib_env.db.all_tracks()[0]
    lib_env.config.acoustid["api_key"] = "test-key"
    lookup = identify_module.get_recording

    def recording(*args, **kwargs):
        if change == "disk":
            with path.open("ab") as target:
                target.write(b"changed")
        else:
            lib_env.db.update_track_columns(track.id, status="deleted")
        return lookup(*args, **kwargs)

    monkeypatch.setattr(identify_module, "get_recording", recording)
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/identify", json={"track_id": track.id})
        assert response.status_code == 400
        assert "changed during" in response.json()["detail"]
    assert lib_env.db.query_one("SELECT COUNT(*) AS n FROM metadata_reviews")["n"] == 0


def test_audio_review_refuses_file_changed_after_identification(lib_env, providers):
    path = lib_env.library / "song.mp3"
    make_mp3(path)
    Scanner(lib_env.config, lib_env.db).scan()
    track = lib_env.db.all_tracks()[0]
    lib_env.config.acoustid["api_key"] = "test-key"
    with TestClient(create_app(lib_env.config)) as client:
        review = client.post("/api/metadata/identify", json={"track_id": track.id}).json()
        with path.open("ab") as target:
            target.write(b"changed")
        before = file_hash(str(path))
        response = client.post("/api/metadata/apply", json={
            "review_id": review["review_id"], "candidate_index": 0, "fields": ["title"],
        })
        assert response.status_code == 400
        assert file_hash(str(path)) == before


def test_audio_provider_failure_returns_error_without_creating_review(lib_env, providers, monkeypatch):
    path = lib_env.library / "song.mp3"
    make_mp3(path)
    Scanner(lib_env.config, lib_env.db).scan()
    track = lib_env.db.all_tracks()[0]
    lib_env.config.acoustid["api_key"] = "test-key"

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("AcoustID did not respond")

    monkeypatch.setattr(identify_module, "catalog_post", timeout)
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/identify", json={"track_id": track.id})
        assert response.status_code == 502
        assert "did not respond" in response.json()["detail"]
        assert "test-key" not in response.text
    assert lib_env.db.query_one("SELECT COUNT(*) AS n FROM metadata_reviews")["n"] == 0


def test_audio_identification_shares_lookup_lane_but_does_not_block_manual_edit_lane(lib_env, providers, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    path = lib_env.library / "song.mp3"
    make_mp3(path)
    Scanner(lib_env.config, lib_env.db).scan()
    track = lib_env.db.all_tracks()[0]
    lib_env.config.acoustid["api_key"] = "test-key"
    entered, finish = threading.Event(), threading.Event()
    lookup = identify_module.get_recording

    def recording(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return lookup(*args, **kwargs)

    monkeypatch.setattr(identify_module, "get_recording", recording)
    with TestClient(create_app(lib_env.config)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/metadata/identify", json={"track_id": track.id})
        try:
            assert entered.wait(10)
            assert client.post("/api/metadata/lookup", json={"track_id": track.id}).status_code == 409
            assert client.post("/api/metadata/identify", json={"track_id": track.id}).status_code == 409
            assert client.post("/api/metadata/track-edit/preview", json={
                "track_id": track.id, "fields": ["title"], "values": {"title": "Manual"},
            }).status_code == 200
        finally:
            finish.set()
        assert future.result(timeout=10).status_code == 200
