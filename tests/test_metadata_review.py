import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import musicbrainzngs
import pytest
from conftest import JPEG_600x400, make_flac, make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

import musiclib.metadata_review as review_module
from musiclib.metadata_review import MetadataReview, lookup_track, search_itunes
from musiclib.models import Issue, Track
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


@pytest.fixture(autouse=True)
def _empty_itunes(monkeypatch):
    monkeypatch.setattr(
        "musiclib.metadata_review.httpx.get",
        lambda *args, **kwargs: httpx.Response(
            200, json={"results": []}, request=httpx.Request("GET", args[0])
        ),
    )


def _musicbrainz_result():
    return {
        "recording-list": [{
            "id": "recording-1",
            "title": "Correct Song",
            "ext:score": "90",
            "artist-credit": [{"artist": {"name": "Correct Artist"}}],
            "release-list": [{
                "id": "release-1", "title": "Correct Album", "date": "2004-01-02",
                "artist-credit": [{"artist": {"name": "Correct Artist"}}],
                "medium-list": [{"position": "1", "track-list": [
                    {"position": "1", "title": "Another Song"},
                    {"position": "3", "title": "Correct Song",
                     "recording": {"id": "recording-1"}},
                ]}],
            }],
        }],
    }


def test_lookup_provides_source_and_confidence(monkeypatch):

    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings",
        lambda **kwargs: _musicbrainz_result(),
    )
    results = lookup_track(Track(title="Correct Song", artist="Correct Artist"))
    assert results[0]["source"] == "MusicBrainz"
    assert results[0]["confidence"] == 100
    assert results[0]["fields"]["album"] == "Correct Album"
    assert results[0]["fields"]["track_no"] == "3"
    assert results[0]["fields"]["mbid_track"] == "recording-1"
    assert results[0]["cover_url"] == "https://coverartarchive.org/release/release-1/front-250"


def test_lookup_rejects_malformed_musicbrainz_response(monkeypatch):
    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings", lambda **kwargs: None
    )
    try:
        lookup_track(Track(title="Song", artist="Artist"))
    except RuntimeError as exc:
        assert "unexpected response" in str(exc)
    else:
        raise AssertionError("Expected malformed MusicBrainz response to be rejected")


def test_lookup_ignores_malformed_optional_release_fields(monkeypatch):
    response = _musicbrainz_result()
    recording = response["recording-list"][0]
    recording["artist-credit"] = [{"artist": "unexpected"}]
    recording["release-list"][0]["artist-credit"] = [{"artist": None}]
    recording["release-list"][0]["medium-list"] = [None, "unexpected"]
    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings",
        lambda **kwargs: response,
    )
    results = lookup_track(Track(title="Correct Song", artist="Original Artist"))
    assert results[0]["fields"]["artist"] == "Original Artist"
    assert results[0]["fields"]["album_artist"] == "Original Artist"
    assert results[0]["fields"]["track_no"] is None


def test_lookup_api_and_apply_only_explicitly_selected_tags(lib_env, monkeypatch):
    env = lib_env
    path = env.library / "wrong.mp3"
    make_mp3(path, tags={"title": "Wrong Song", "artist": "Wrong Artist"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings",
        lambda **kwargs: _musicbrainz_result(),
    )
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/lookup", json={"track_id": track.id})
        assert response.status_code == 200
        review = response.json()
        assert review["candidates"][0]["source"] == "MusicBrainz"
        failed = client.post("/api/metadata/apply", json={
            "review_id": review["review_id"], "candidate_index": 0, "fields": [],
        })
        assert failed.status_code == 400
        applied = client.post("/api/metadata/apply", json={
            "review_id": review["review_id"], "candidate_index": 0,
            "fields": ["title", "album"],
        })
        assert applied.status_code == 200
        assert applied.json()["fields"] == {"title": "Correct Song", "album": "Correct Album"}
        updated = env.db.get_track(track.id)
        assert updated.title == "Correct Song"
        assert updated.album == "Correct Album"
        assert updated.artist == "Wrong Artist"
        assert env.db.query_one(
            "SELECT status FROM metadata_reviews WHERE id=?", (review["review_id"],)
        )["status"] == "applied"
        assert client.post("/api/metadata/apply", json={
            "review_id": review["review_id"], "candidate_index": 0, "fields": ["artist"],
        }).status_code == 400


def test_review_refuses_file_changed_after_lookup(lib_env, monkeypatch):
    env = lib_env
    path = env.library / "mutable.mp3"
    make_mp3(path, tags={"title": "Wrong Song", "artist": "Wrong Artist"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings",
        lambda **kwargs: _musicbrainz_result(),
    )
    review = MetadataReview(env.config, env.db).propose(track.id)
    with path.open("ab") as audio_file:
        audio_file.write(b"changed")
    try:
        MetadataReview(env.config, env.db).apply(review["review_id"], 0, ["title"])
    except ValueError as exc:
        assert "changed since lookup" in str(exc)
    else:
        raise AssertionError("Expected stale metadata review to be refused")


def test_review_rejects_bad_candidate_or_field_without_changing_file(lib_env, monkeypatch):
    env = lib_env
    path = env.library / "song.mp3"
    make_mp3(path, tags={"title": "Wrong Song", "artist": "Wrong Artist"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings",
        lambda **kwargs: _musicbrainz_result(),
    )
    review = MetadataReview(env.config, env.db).propose(track.id)
    before = path.read_bytes()
    for candidate, fields in ((7, ["title"]), (0, ["path"])):
        try:
            MetadataReview(env.config, env.db).apply(review["review_id"], candidate, fields)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected invalid review input to be refused")
        assert path.read_bytes() == before


def test_first_lookup_configures_client(lib_env, monkeypatch):
    env = lib_env
    make_mp3(env.library / "song.mp3", tags={"title": "Song"})
    Scanner(env.config, env.db).scan()
    monkeypatch.setattr(musicbrainzngs.musicbrainz, "_useragent", "")
    env.config.brainz["contact"] = "https://github.com/fatannasty/musiclib"

    def search(**kwargs):
        assert "musiclib/0.1" in musicbrainzngs.musicbrainz._useragent
        assert env.config.brainz["contact"] in musicbrainzngs.musicbrainz._useragent
        return _musicbrainz_result()

    monkeypatch.setattr(review_module, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/lookup", json={"track_id": env.db.all_tracks()[0].id})
        assert response.status_code == 200


@pytest.mark.parametrize("title,artist", [
    ("Correct Song", "Correct Artist"), ("Correct Song", ""), ("", "Correct Artist"),
])
def test_manual_lookup_for_untagged_file_preserves_tags(lib_env, monkeypatch, title, artist):
    env = lib_env
    path = env.library / "untagged.mp3"
    make_mp3(path)
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    before = path.read_bytes()
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return _musicbrainz_result()

    monkeypatch.setattr(review_module, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/lookup", json={
            "track_id": track.id, "title": f" {title} ", "artist": f" {artist} ",
        })
        assert response.status_code == 200
        assert response.json()["track"]["title"] is None
        assert response.json()["track"]["artist"] is None
    expected = {"limit": 6}
    if title:
        expected["recording"] = title
    if artist:
        expected["artist"] = artist
    assert calls == [expected]
    assert path.read_bytes() == before
    assert env.db.get_track(track.id).title is None


def test_manual_lookup_can_override_existing_tags(lib_env, monkeypatch):
    env = lib_env
    make_mp3(env.library / "song.mp3", tags={"title": "Wrong Song", "artist": "Wrong Artist"})
    Scanner(env.config, env.db).scan()
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return {"recording-list": []}

    monkeypatch.setattr(review_module, "search_recordings", search)
    MetadataReview(env.config, env.db).propose(
        env.db.all_tracks()[0].id, title="Correct Song", artist=""
    )
    assert calls == [{"limit": 6, "recording": "Correct Song"}]


def test_empty_manual_search_is_actionable_without_provider_call(lib_env, monkeypatch):
    env = lib_env
    make_mp3(env.library / "untagged.mp3")
    Scanner(env.config, env.db).scan()

    def search(**kwargs):
        pytest.fail("Empty search must not call MusicBrainz")

    monkeypatch.setattr(review_module, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/lookup", json={
            "track_id": env.db.all_tracks()[0].id, "title": " ", "artist": " ",
        })
        assert response.status_code == 400
        assert "Enter a title or artist" in response.json()["detail"]


@pytest.mark.parametrize("error", [
    musicbrainzngs.NetworkError("connection timed out"),
    musicbrainzngs.UsageError("client configuration error"),
])
def test_provider_failure_has_actionable_api_error(lib_env, monkeypatch, error):
    env = lib_env
    path = env.library / "song.mp3"
    make_mp3(path, tags={"title": "Song"})
    Scanner(env.config, env.db).scan()
    before = path.read_bytes()

    def search(**kwargs):
        raise error

    monkeypatch.setattr(review_module, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/lookup", json={"track_id": env.db.all_tracks()[0].id})
        assert response.status_code == 502
        assert "MusicBrainz lookup failed" in response.json()["detail"]
        assert str(error) in response.json()["detail"]
    assert path.read_bytes() == before


def test_bulk_lookup_preserves_files_and_saves_matches_empty_results_and_errors(lib_env, monkeypatch):
    env = lib_env
    for name, title in (("match", "Song"), ("empty", "No match"), ("untagged", "")):
        make_mp3(env.library / f"{name}.mp3", tags={"title": title})
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    before = {track.path: Path(track.path).read_bytes() for track in tracks}

    def search(**kwargs):
        return {"recording-list": []} if kwargs["recording"] == "No match" else _musicbrainz_result()

    monkeypatch.setattr(review_module, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/lookup-bulk", json={
            "track_ids": [track.id for track in tracks],
        })
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert job["completed"] == job["total"] == 3
        assert job["summary"] == {"reviews": 2, "errors": 1}
        reviews = client.get(f"/api/jobs/{job_id}/results/reviews").json()["items"]
        assert sorted(len(review["candidates"]) for review in reviews) == [0, 1]
        errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
        assert "Enter a title or artist" in errors[0]["error"]
    with TestClient(create_app(env.config)) as client:
        assert client.get(f"/api/jobs/{job_id}/results/reviews").json()["items"] == reviews
    for track in tracks:
        assert Path(track.path).read_bytes() == before[track.path]


def test_bulk_apply_only_selected_fields_and_reports_stale_files(lib_env, monkeypatch):
    env = lib_env
    for index in range(3):
        make_mp3(env.library / f"{index}.mp3", tags={
            "title": f"Original {index}", "artist": "Original Artist",
        })
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    monkeypatch.setattr(
        review_module, "search_recordings", lambda **kwargs: _musicbrainz_result()
    )
    reviews = [MetadataReview(env.config, env.db).propose(track.id) for track in tracks]
    with open(tracks[1].path, "ab") as audio_file:
        audio_file.write(b"changed")
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/apply-bulk", json={"reviews": [
            {"review_id": review["review_id"], "candidate_index": 0, "fields": ["title"]}
            for review in reviews[:2]
        ]})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert job["completed"] == job["total"] == 2
        assert job["summary"] == {"applied": 1, "errors": 1}
        errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
        assert errors[0]["review_id"] == reviews[1]["review_id"]
        assert "changed since lookup" in errors[0]["error"]
        assert env.db.get_track(tracks[0].id).title == "Correct Song"
        assert env.db.get_track(tracks[0].id).artist == "Original Artist"
        assert env.db.get_track(tracks[1].id).title == tracks[1].title
        assert env.db.get_track(tracks[2].id).title == tracks[2].title
        retry = client.post("/api/metadata/apply-bulk", json={"reviews": [{
            "review_id": reviews[0]["review_id"], "candidate_index": 0, "fields": ["artist"],
        }]})
        retry_id = retry.json()["job_id"]
        assert wait_job(client, retry_id)["summary"] == {"applied": 0, "errors": 1}
        assert env.db.get_track(tracks[0].id).artist == "Original Artist"


@pytest.mark.parametrize("track_ids", [[], [1, 1], list(range(101))])
def test_bulk_lookup_rejects_empty_duplicate_and_oversized_batches(lib_env, track_ids):
    with TestClient(create_app(lib_env.config)) as client:
        assert client.post("/api/metadata/lookup-bulk", json={
            "track_ids": track_ids,
        }).status_code == 400
        assert client.get("/api/jobs").json() == []


@pytest.mark.parametrize("reviews", [
    [],
    [{"review_id": 1, "candidate_index": 0, "fields": []}],
    [{"review_id": 1, "candidate_index": 0, "fields": ["path"]}],
    [{"review_id": 1, "candidate_index": -1, "fields": ["title"]}],
    [{"review_id": 1, "candidate_index": 0, "fields": ["title"]}] * 2,
    [{"review_id": index, "candidate_index": 0, "fields": ["title"]} for index in range(101)],
])
def test_bulk_apply_rejects_invalid_batches_before_starting(lib_env, reviews):
    with TestClient(create_app(lib_env.config)) as client:
        assert client.post("/api/metadata/apply-bulk", json={
            "reviews": reviews,
        }).status_code == 400
        assert client.get("/api/jobs").json() == []


def test_bulk_provider_failure_continues_without_holding_library_lock(lib_env, monkeypatch):
    env = lib_env
    for title in ("Failure", "Success"):
        make_mp3(env.library / f"{title}.mp3", tags={"title": title})
    Scanner(env.config, env.db).scan()
    entered, finish = threading.Event(), threading.Event()

    def search(**kwargs):
        if kwargs["recording"] == "Failure":
            raise musicbrainzngs.NetworkError("provider unavailable")
        entered.set()
        assert finish.wait(5)
        return _musicbrainz_result()

    monkeypatch.setattr(review_module, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        try:
            response = client.post("/api/metadata/lookup-bulk", json={
                "track_ids": [track.id for track in env.db.all_tracks()],
            })
            assert response.status_code == 200
            job_id = response.json()["job_id"]
            assert entered.wait(5)
            assert client.get(f"/api/jobs/{job_id}").json()["read_only"] is True
            assert client.post("/api/metadata/lookup", json={"track_id": 1}).status_code == 409
            assert client.post("/api/metadata/lookup-bulk", json={"track_ids": [1]}).status_code == 409
            assert client.post("/api/metadata/album-suggest", json={"track_ids": [1]}).status_code == 409
            apply_response = client.post("/api/metadata/apply-bulk", json={"reviews": [{
                "review_id": 1, "candidate_index": 0, "fields": ["title"],
            }]})
            assert apply_response.status_code == 200
            assert wait_job(client, apply_response.json()["job_id"])["status"] == "done"
        finally:
            finish.set()
        assert wait_job(client, job_id)["summary"] == {"reviews": 1, "errors": 1}
        error = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"][0]
        assert "provider unavailable" in error["error"]


@pytest.mark.parametrize("album_search", [False, True])
def test_write_during_search_is_allowed_but_stale_results_cannot_overwrite_it(
    lib_env, monkeypatch, album_search,
):
    env = lib_env
    path = env.library / "song.mp3"
    make_mp3(path, tags={"title": "Song", "artist": "Artist", "album": "Original Album"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    entered, finish = threading.Event(), threading.Event()

    def delayed_search(**kwargs):
        entered.set()
        assert finish.wait(5)
        result = _musicbrainz_result()
        return (
            {"release-list": result["recording-list"][0]["release-list"]}
            if album_search else result
        )

    monkeypatch.setattr(
        review_module, "search_releases" if album_search else "search_recordings", delayed_search
    )
    with TestClient(create_app(env.config)) as client:
        preview = client.post("/api/metadata/album-edit/preview", json={
            "track_ids": [track.id], "fields": ["album"], "values": {"album": "Fresh edit"},
        }).json()
        with ThreadPoolExecutor(max_workers=1) as pool:
            lookup = pool.submit(
                client.post,
                "/api/metadata/album-suggest" if album_search else "/api/metadata/lookup",
                json={"track_ids": [track.id]} if album_search else {"track_id": track.id},
            )
            try:
                assert entered.wait(5)
                applied = client.post("/api/metadata/album-edit/apply", json={"reviews": [{
                    "review_id": preview["reviews"][0]["review_id"],
                    "candidate_index": 0, "fields": ["album"],
                }]})
                assert applied.status_code == 200
                assert wait_job(client, applied.json()["job_id"])["summary"]["applied"] == 1
                assert env.db.get_track(track.id).album == "Fresh edit"
            finally:
                finish.set()
            response = lookup.result(timeout=5)
        assert response.status_code == 200
        payload = response.json()
        before = path.read_bytes()
        if album_search:
            snapshot = payload["tracks"][0]
            assert snapshot["file_hash"] == track.file_hash
            result = client.post("/api/metadata/album-apply", json={
                "track_ids": [track.id], "fields": ["album"], "values": {"album": "Stale album"},
                "snapshots": {track.id: {
                    "path": snapshot["path"], "file_hash": snapshot["file_hash"],
                }},
            })
            assert result.status_code == 200
            job_id = result.json()["job_id"]
            assert wait_job(client, job_id)["summary"]["applied"] == 0
            errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
            assert "changed since album search" in errors[0]["error"]
        else:
            result = client.post("/api/metadata/apply", json={
                "review_id": payload["review_id"], "candidate_index": 0, "fields": ["album"],
            })
            assert result.status_code == 400
            assert "changed since lookup" in result.json()["detail"]
        assert path.read_bytes() == before
        assert env.db.get_track(track.id).album == "Fresh edit"


@pytest.mark.parametrize("local_match", [True, False])
def test_album_provider_failures_are_visible_with_or_without_library_matches(
    lib_env, monkeypatch, local_match,
):
    env = lib_env
    make_mp3(env.library / "song.mp3", tags={
        "title": "Song", "artist": "Artist", "album": "Album" if local_match else "",
    })
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]

    def fail(**kwargs):
        raise musicbrainzngs.NetworkError("provider unavailable")

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("iTunes timed out")

    monkeypatch.setattr(review_module, "search_releases", fail)
    monkeypatch.setattr(review_module.httpx, "get", timeout)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/album-suggest", json={
            "track_ids": [track.id], "artist": "Artist" if local_match else "Unknown",
            "album": "Album" if local_match else "Unknown",
        })
    assert response.status_code == (200 if local_match else 502)
    if local_match:
        assert response.json()["suggestions"][0]["source"] == "Your library"
        warnings = response.json()["warnings"]
        assert len(warnings) == 2
        assert "provider unavailable" in warnings[0]
        assert "iTunes timed out" in warnings[1]
    else:
        assert "provider unavailable" in response.json()["detail"]
        assert "iTunes timed out" in response.json()["detail"]


def _itunes_song(**overrides):
    item = {
        "trackName": "Correct Song",
        "artistName": "Correct Artist",
        "collectionName": "Correct Album",
        "collectionArtistName": "Correct Artist",
        "releaseDate": "2004-01-02T12:00:00Z",
        "trackNumber": 3,
        "discNumber": 1,
        "primaryGenreName": "Rock",
        "artworkUrl100": "https://example.test/cover/100x100bb.jpg",
    }
    item.update(overrides)
    return item


def test_itunes_cover_merges_with_musicbrainz_match(monkeypatch):
    monkeypatch.setattr(
        "musiclib.metadata_review.search_recordings",
        lambda **kwargs: _musicbrainz_result(),
    )
    monkeypatch.setattr(
        "musiclib.metadata_review.search_itunes",
        lambda *args, **kwargs: [
            {
                "source": "iTunes", "confidence": 90, "cover_url": "https://example.test/cover/300x300bb.jpg",
                "cover_fallback": None, "recording_mbid": None, "release_mbid": None,
                "fields": {
                    "title": "Correct Song", "artist": "Correct Artist", "album": "Correct Album",
                    "album_artist": "Correct Artist", "year": 2004, "track_no": 3, "disc_no": 1,
                    "genre": "Rock", "mbid_track": None, "mbid_release": None,
                },
            }
        ],
    )
    results = lookup_track(Track(title="Correct Song", artist="Correct Artist"))
    assert len(results) == 1
    assert results[0]["source"] == "MusicBrainz + iTunes"
    assert results[0]["cover_url"] == "https://example.test/cover/300x300bb.jpg"
    assert results[0]["cover_fallback"] == "https://coverartarchive.org/release/release-1/front-250"
    assert results[0]["fields"]["genre"] == "Rock"
    assert results[0]["fields"]["mbid_track"] == "recording-1"


def test_album_suggestions_rank_existing_library_albums(lib_env, monkeypatch):
    env = lib_env
    for index in range(2):
        make_mp3(env.library / f"night-{index}.mp3", tags={
            "title": f"Night {index}", "artist": "Known Artist",
            "album": "Night Songs", "albumartist": "Known Artist", "date": "2001",
        })
    make_mp3(env.library / "other.mp3", tags={
        "title": "Other", "artist": "Other Artist", "album": "Other Album",
    })
    make_mp3(env.library / "new.mp3", tags={
        "title": "New Track", "artist": "Known Artist", "album": "Wrong Album",
    })
    Scanner(env.config, env.db).scan()
    monkeypatch.setattr(
        "musiclib.metadata_review.search_releases",
        lambda **kwargs: {"release-list": [{
            "id": "release-2", "title": "Night Songs Deluxe", "ext:score": "80",
            "date": "2001-05-01", "artist-credit": [{"artist": {"name": "Known Artist"}}],
        }]},
    )
    tracks = [track for track in env.db.all_tracks() if track.title == "New Track"]
    before = Path(tracks[0].path).read_bytes()
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/album-suggest", json={
            "track_ids": [tracks[0].id], "album": "Night", "artist": "Known Artist",
        })
    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert suggestions[0]["source"] == "Your library"
    assert suggestions[0]["fields"]["album"] == "Night Songs"
    assert suggestions[0]["in_library"] == 2
    assert any(item["source"] == "MusicBrainz" for item in suggestions)
    assert Path(tracks[0].path).read_bytes() == before


def test_album_apply_writes_only_chosen_fields(lib_env, monkeypatch):
    env = lib_env
    for index in range(2):
        make_mp3(env.library / f"track-{index}.mp3", tags={
            "title": f"Song {index}", "artist": "Old Artist", "album": "Old Album",
        })
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    monkeypatch.setattr("musiclib.metadata_review.search_releases", lambda **kwargs: {"release-list": []})
    with TestClient(create_app(env.config)) as client:
        rejected = client.post("/api/metadata/album-apply", json={
            "track_ids": [track.id for track in tracks], "fields": [], "values": {},
        })
        assert rejected.status_code == 400
        assert client.get("/api/jobs").json() == []
        response = client.post("/api/metadata/album-apply", json={
            "track_ids": [track.id for track in tracks],
            "fields": ["album", "year"],
            "values": {"album": "New Album", "year": 1999, "artist": "Should Ignore"},
            "snapshots": {
                track.id: {"path": track.path, "file_hash": track.file_hash}
                for track in tracks
            },
        })
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "done"
        assert job["summary"]["applied"] == 2
        assert job["summary"]["errors"] == 0
    for track in tracks:
        updated = env.db.get_track(track.id)
        assert updated.album == "New Album"
        assert updated.year == 1999
        assert updated.title == track.title
        assert updated.artist == "Old Artist"


@pytest.mark.parametrize("change", ["moved", "modified-on-disk"])
def test_album_apply_skips_stale_snapshots_without_stopping_other_files(lib_env, change):
    env = lib_env
    for index in range(2):
        make_mp3(env.library / f"song-{index}.mp3", tags={
            "title": f"Song {index}", "artist": "Artist", "album": "Original",
        })
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    snapshots = {
        track.id: {"path": track.path, "file_hash": track.file_hash} for track in tracks
    }
    changed = tracks[0]
    changed_path = Path(changed.path)
    if change == "moved":
        moved = env.library / "moved.mp3"
        changed_path.rename(moved)
        env.db.update_track_columns(changed.id, path=str(moved))
        changed_path = moved
    else:
        with changed_path.open("ab") as file:
            file.write(b"changed")
    before = changed_path.read_bytes()
    with TestClient(create_app(env.config)) as client:
        body = {
            "track_ids": [track.id for track in tracks], "fields": ["album"],
            "values": {"album": "Updated"}, "snapshots": snapshots,
        }
        rejected = client.post("/api/metadata/album-apply", json={**body, "snapshots": {}})
        assert rejected.status_code == 400
        assert client.get("/api/jobs").json() == []
        response = client.post("/api/metadata/album-apply", json=body)
        job_id = response.json()["job_id"]
        summary = wait_job(client, job_id)["summary"]
        assert summary["applied"] == summary["errors"] == 1
        errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
        assert errors[0]["track_id"] == changed.id
        assert "changed since album search" in errors[0]["error"]
    assert changed_path.read_bytes() == before
    assert env.db.get_track(changed.id).album == "Original"
    assert env.db.get_track(tracks[1].id).album == "Updated"


def test_manual_album_edit_preview_and_apply_preserve_unselected_tags(lib_env, monkeypatch):
    env = lib_env
    for index in range(2):
        make_mp3(env.library / f"manual-{index}.mp3", tags={
            "title": f"Song {index}", "artist": f"Performer {index}",
            "album": "Wrong Album", "tracknumber": index + 1,
        })
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    before = {track.id: Path(track.path).read_bytes() for track in tracks}

    def no_network(*args, **kwargs):
        pytest.fail("Manual editing must not query a metadata provider")

    monkeypatch.setattr(review_module, "search_recordings", no_network)
    monkeypatch.setattr(review_module, "search_releases", no_network)
    monkeypatch.setattr("musiclib.metadata_review.httpx.get", no_network)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/album-edit/preview", json={
            "track_ids": [track.id for track in tracks],
            "fields": ["album", "album_artist", "year"],
            "values": {"album": " Correct Album ", "album_artist": "Various Artists",
                       "year": "2004", "artist": "Must not write"},
        })
        assert response.status_code == 200
        preview = response.json()
        assert len(preview["reviews"]) == 2
        assert preview["errors"] == []
        for review in preview["reviews"]:
            assert review["track"]["album"] == "Wrong Album"
            assert review["candidates"][0]["fields"] == {
                "album": "Correct Album", "album_artist": "Various Artists", "year": 2004,
            }
        for track in tracks:
            assert Path(track.path).read_bytes() == before[track.id]
        batch = {"reviews": [
            {"review_id": review["review_id"], "candidate_index": 0, "fields": preview["fields"]}
            for review in preview["reviews"]
        ]}
        job_id = client.post("/api/metadata/album-edit/apply", json=batch).json()["job_id"]
        assert wait_job(client, job_id)["summary"] == {"applied": 2, "errors": 0}
        retry_id = client.post("/api/metadata/album-edit/apply", json=batch).json()["job_id"]
        assert wait_job(client, retry_id)["summary"] == {"applied": 0, "errors": 2}
    for track in tracks:
        updated = env.db.get_track(track.id)
        assert updated.album == "Correct Album"
        assert updated.album_artist == "Various Artists"
        assert updated.year == 2004
        assert updated.artist == track.artist
        assert updated.title == track.title
        assert updated.track_no == track.track_no
        assert updated.path == track.path


@pytest.mark.parametrize(("fields", "values"), [
    ([], {}),
    (["title"], {"title": "Wrong scope"}),
    (["album", "album"], {"album": "Duplicate"}),
    (["album"], {"album": "  "}),
    (["album"], {"album": ["Not text"]}),
    (["album"], {"album": "x" * 1001}),
    (["year"], {"year": True}),
    (["year"], {"year": 2001.5}),
    (["year"], {"year": "not a year"}),
    (["year"], {"year": 0}),
    (["year"], {"year": 10000}),
])
def test_manual_album_edit_rejects_invalid_preview(lib_env, fields, values):
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/album-edit/preview", json={
            "track_ids": [1], "fields": fields, "values": values,
        })
        assert response.status_code == 400
        assert lib_env.db.query_one("SELECT COUNT(*) AS n FROM metadata_reviews")["n"] == 0
        assert client.get("/api/jobs").json() == []


def test_manual_album_edit_skips_changed_and_unwritable_files(lib_env, monkeypatch):
    env = lib_env
    for index in range(3):
        make_mp3(env.library / f"manual-{index}.mp3", tags={
            "title": f"Song {index}", "album": "Original",
        })
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    reviewer = MetadataReview(env.config, env.db)
    preview = reviewer.preview_album_edit(
        [track.id for track in tracks], ["album"], {"album": "New"},
    )
    with open(tracks[0].path, "ab") as file:
        file.write(b"changed externally")
    from musiclib.metadata_review import write_audio_tags

    def permission_error(path, values, **kwargs):
        if str(path) == tracks[1].path:
            raise PermissionError("Read-only music folder")
        return write_audio_tags(path, values, **kwargs)

    monkeypatch.setattr("musiclib.metadata_review.write_audio_tags", permission_error)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/album-edit/apply", json={"reviews": [
            {"review_id": review["review_id"], "candidate_index": 0, "fields": ["album"]}
            for review in preview["reviews"]
        ]})
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"] == {"applied": 1, "errors": 2}
        errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
        assert "changed since lookup" in errors[0]["error"]
        assert "Read-only" in errors[1]["error"]
    assert env.db.get_track(tracks[0].id).album == "Original"
    assert env.db.get_track(tracks[1].id).album == "Original"
    assert env.db.get_track(tracks[2].id).album == "New"


def test_manual_album_edit_missing_files_and_batch_limit(lib_env):
    env = lib_env
    make_mp3(env.library / "missing.mp3", tags={"title": "Missing"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    Path(track.path).unlink()
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/album-edit/preview", json={
            "track_ids": [track.id], "fields": ["album"], "values": {"album": "New"},
        })
        assert response.json()["reviews"] == []
        assert "missing" in response.json()["errors"][0]["error"]
        for ids in ([], [track.id] * 2, list(range(201))):
            assert client.post("/api/metadata/album-edit/preview", json={
                "track_ids": ids, "fields": ["album"], "values": {"album": "New"},
            }).status_code == 400
        # Two hundred is accepted by the manual editor without changing the catalog batch limit.
        batch = {"reviews": [
            {"review_id": index, "candidate_index": 0, "fields": ["album"]}
            for index in range(200)
        ]}
        response = client.post("/api/metadata/album-edit/apply", json=batch)
        assert response.status_code == 200
        assert wait_job(client, response.json()["job_id"])["summary"]["errors"] == 200
        assert client.post("/api/metadata/apply-bulk", json=batch).status_code == 400


@pytest.mark.parametrize("extension", ["mp3", "flac"])
def test_manual_track_edit_is_reviewed_and_preserves_other_data(lib_env, monkeypatch, extension):
    import mutagen

    from musiclib.artwork import embed_cover, read_cover

    env = lib_env
    path = env.library / f"track.{extension}"
    if extension == "mp3":
        make_mp3(path, tags={"title": "Original", "artist": "Performer", "album": "Album"})
    else:
        make_flac(path, tags={"TITLE": "Original", "ARTIST": "Performer", "ALBUM": "Album"})
    audio = mutagen.File(path, easy=True)
    audio["tracknumber"] = ["1/12"]
    audio["discnumber"] = ["1/2"]
    audio["genre"] = ["Jazz"]
    audio.save()
    embed_cover(path, JPEG_600x400)
    sibling = env.library / "untouched.mp3"
    make_mp3(sibling, tags={"title": "Sibling", "artist": "Performer", "album": "Album"})
    Scanner(env.config, env.db, workers=1).scan()
    track = env.db.find_track_by_path(str(path))
    track.issues = [Issue("decode_error", "Existing decoder finding", "bad")]
    track.status = "bad"
    env.db.upsert_track(track)
    before, sibling_before = path.read_bytes(), sibling.read_bytes()
    tags_before = dict(mutagen.File(path, easy=True))
    cover_before = read_cover(str(path), include_folder=False)

    def no_network(*args, **kwargs):
        pytest.fail("Manual track editing must not query a metadata provider")

    monkeypatch.setattr(review_module, "search_recordings", no_network)
    monkeypatch.setattr(review_module, "search_releases", no_network)
    monkeypatch.setattr(review_module.httpx, "get", no_network)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/track-edit/preview", json={
            "track_id": track.id, "fields": ["title", "track_no", "disc_no"],
            "values": {"title": " Corrected title ", "track_no": " 003 ", "disc_no": 2,
                       "artist": "Unchecked artist"},
        })
        assert response.status_code == 200
        preview = response.json()
        assert preview["errors"] == []
        review = preview["reviews"][0]
        assert review["track"]["title"] == "Original"
        assert (review["track"]["track_total"], review["track"]["disc_total"]) == (12, 2)
        assert review["candidates"][0]["fields"] == {
            "title": "Corrected title", "track_no": 3, "disc_no": 2,
        }
        assert path.read_bytes() == before
        assert sibling.read_bytes() == sibling_before
        body = {"review_id": review["review_id"], "candidate_index": 0, "fields": preview["fields"]}
        rejected = client.post("/api/metadata/track-edit/apply", json={**body, "fields": ["album"]})
        assert rejected.status_code == 400
        assert path.read_bytes() == before
        response = client.post("/api/metadata/track-edit/apply", json=body)
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"] == {"applied": 1, "errors": 0}
        retry = client.post("/api/metadata/track-edit/apply", json=body)
        assert wait_job(client, retry.json()["job_id"])["summary"] == {"applied": 0, "errors": 1}
    updated = env.db.get_track(track.id)
    assert (updated.title, updated.track_no, updated.disc_no) == ("Corrected title", 3, 2)
    assert (updated.track_total, updated.disc_total) == (12, 2)
    assert updated.path == str(path)
    assert updated.file_hash != track.file_hash
    assert updated.status == "bad"
    assert updated.issues == track.issues
    tags_after = dict(mutagen.File(path, easy=True))
    assert tags_after.pop("title") == ["Corrected title"]
    assert tags_after.pop("tracknumber") == ["3/12"]
    assert tags_after.pop("discnumber") == ["2/2"]
    for key in ("title", "tracknumber", "discnumber"):
        tags_before.pop(key)
    assert tags_after == tags_before
    assert read_cover(str(path), include_folder=False) == cover_before
    assert sibling.read_bytes() == sibling_before


@pytest.mark.parametrize(("fields", "values"), [
    ([], {}),
    (["title", "title"], {"title": "Duplicate"}),
    (["album"], {"album": "Wrong scope"}),
    (["title"], {"title": " "}),
    (["title"], {"title": "x" * 1001}),
    (["track_no"], {"track_no": 0}),
    (["track_no"], {"track_no": 10000}),
    (["disc_no"], {"disc_no": True}),
    (["disc_no"], {"disc_no": "1.5"}),
    (["track_no"], {"track_no": "3/12"}),
])
def test_manual_track_edit_rejects_invalid_preview(lib_env, fields, values):
    with TestClient(create_app(lib_env.config)) as client:
        response = client.post("/api/metadata/track-edit/preview", json={
            "track_id": 1, "fields": fields, "values": values,
        })
        assert response.status_code == 400
        assert lib_env.db.query_one("SELECT COUNT(*) AS n FROM metadata_reviews")["n"] == 0


def test_manual_track_edit_numbers_without_totals_and_missing_tag_refresh(lib_env):
    import mutagen

    env = lib_env
    path = env.library / "untitled.mp3"
    make_mp3(path, tags={"artist": "Artist", "album": "Album"})
    Scanner(env.config, env.db, workers=1).scan()
    track = env.db.all_tracks()[0]
    track.issues.append(Issue("too_short", "Existing duration warning", "warn"))
    env.db.upsert_track(track)
    with TestClient(create_app(env.config)) as client:
        preview = client.post("/api/metadata/track-edit/preview", json={
            "track_id": track.id, "fields": ["title", "artist", "track_no", "disc_no"],
            "values": {"title": "Named", "artist": "New performer", "track_no": 9999, "disc_no": 1},
        }).json()
        response = client.post("/api/metadata/track-edit/apply", json={
            "review_id": preview["reviews"][0]["review_id"],
            "candidate_index": 0, "fields": preview["fields"],
        })
        assert wait_job(client, response.json()["job_id"])["summary"] == {"applied": 1, "errors": 0}
    updated = env.db.get_track(track.id)
    assert (updated.title, updated.artist, updated.track_no, updated.disc_no) == (
        "Named", "New performer", 9999, 1,
    )
    assert (updated.track_total, updated.disc_total) == (None, None)
    assert updated.status == "suspect"
    assert updated.issues == [Issue("too_short", "Existing duration warning", "warn")]
    assert mutagen.File(path, easy=True)["tracknumber"] == ["9999"]
    assert mutagen.File(path, easy=True)["discnumber"] == ["1"]


@pytest.mark.parametrize("change", ["changed", "moved", "symlink"])
def test_manual_track_edit_refuses_changed_sources(lib_env, change):
    env = lib_env
    path = env.library / "original.mp3"
    make_mp3(path, tags={"title": "Original", "album": "Album"})
    Scanner(env.config, env.db, workers=1).scan()
    track = env.db.all_tracks()[0]
    preview = MetadataReview(env.config, env.db).preview_track_edit(
        track.id, ["title"], {"title": "Corrected"},
    )
    source = path
    if change == "changed":
        path.write_bytes(path.read_bytes() + b"external change")
    else:
        source = path.rename(env.library / "moved.mp3")
        if change == "moved":
            env.db.update_track_columns(track.id, path=str(source))
        else:
            path.symlink_to(source)
    before = source.read_bytes()
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/track-edit/apply", json={
            "review_id": preview["reviews"][0]["review_id"], "candidate_index": 0, "fields": ["title"],
        })
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"] == {"applied": 0, "errors": 1}
        error = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"][0]["error"]
        assert ("Symbolic links" if change == "symlink" else "changed") in error
    assert source.read_bytes() == before
    assert env.db.get_track(track.id).title == "Original"
    if change == "symlink":
        assert path.is_symlink()
        blocked = MetadataReview(env.config, env.db).preview_track_edit(
            track.id, ["title"], {"title": "Another correction"},
        )
        assert blocked["reviews"] == []
        assert "Symbolic links" in blocked["errors"][0]["error"]


def test_manual_track_edit_preserves_external_change_during_save(lib_env, monkeypatch):
    env = lib_env
    path = env.library / "changing.mp3"
    make_mp3(path, tags={"title": "Original", "album": "Album"})
    Scanner(env.config, env.db, workers=1).scan()
    track = env.db.all_tracks()[0]
    reviewer = MetadataReview(env.config, env.db)
    preview = reviewer.preview_track_edit(track.id, ["title"], {"title": "Correction"})
    externally_changed = path.read_bytes() + b"external change during save"
    scan = review_module.scan_worker

    def change_original(temporary):
        result = scan(temporary)
        path.write_bytes(externally_changed)
        return result

    monkeypatch.setattr(review_module, "scan_worker", change_original)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/track-edit/apply", json={
            "review_id": preview["reviews"][0]["review_id"], "candidate_index": 0, "fields": ["title"],
        })
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"] == {"applied": 0, "errors": 1}
        errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
        assert "changed while saving" in errors[0]["error"]
    assert path.read_bytes() == externally_changed
    assert env.db.get_track(track.id).title == "Original"
    assert list(env.library.glob(".*.musiclib-*")) == []


@pytest.mark.parametrize("disc_no", [None, 2])
def test_album_numbering_uses_reviewed_order_and_preserves_other_tags(lib_env, monkeypatch, disc_no):
    import mutagen

    from musiclib.artwork import embed_cover, read_cover

    env = lib_env
    for index in range(4):
        path = env.library / (f"{index}.mp3" if index % 2 else f"{index}.flac")
        tags = {"title": f"Song {index}", "artist": "Performer", "album": "Album"}
        (make_mp3 if index % 2 else make_flac)(path, tags=tags)
        audio = mutagen.File(path, easy=True)
        audio["tracknumber"] = [f"{index + 1}/12"]
        audio["discnumber"] = ["1/2"]
        audio.save()
        embed_cover(path, JPEG_600x400)
    Scanner(env.config, env.db, workers=1).scan()
    tracks = env.db.all_tracks()
    ordered = [tracks[2], tracks[0], tracks[1]]
    before = {track.id: Path(track.path).read_bytes() for track in tracks}
    tags_before = {track.id: dict(mutagen.File(track.path, easy=True)) for track in tracks}
    covers = {track.id: read_cover(track.path, include_folder=False) for track in tracks}

    def no_network(*args, **kwargs):
        pytest.fail("Numbering must not query a metadata provider")

    monkeypatch.setattr(review_module, "search_recordings", no_network)
    monkeypatch.setattr(review_module, "search_releases", no_network)
    monkeypatch.setattr(review_module.httpx, "get", no_network)
    with TestClient(create_app(env.config)) as client:
        body = {"track_ids": [track.id for track in ordered], "start_no": " 005 "}
        if disc_no is not None:
            body["disc_no"] = disc_no
        response = client.post("/api/metadata/numbering/preview", json=body)
        assert response.status_code == 200
        preview = response.json()
        assert preview["errors"] == []
        assert [review["track"]["id"] for review in preview["reviews"]] == [
            track.id for track in ordered
        ]
        assert [review["candidates"][0]["fields"]["track_no"] for review in preview["reviews"]] == [
            5, 6, 7,
        ]
        assert preview["fields"] == ["track_no"] + (["disc_no"] if disc_no is not None else [])
        assert all(Path(track.path).read_bytes() == before[track.id] for track in tracks)
    # Saved candidates and their file snapshots survive an application restart.
    with TestClient(create_app(env.config)) as client:
        batch = {"reviews": [
            {"review_id": review["review_id"], "candidate_index": 0, "fields": preview["fields"]}
            for review in preview["reviews"]
        ]}
        response = client.post("/api/metadata/numbering/apply", json=batch)
        assert response.status_code == 200
        assert wait_job(client, response.json()["job_id"])["summary"] == {"applied": 3, "errors": 0}
        response = client.post("/api/metadata/numbering/apply", json=batch)
        assert wait_job(client, response.json()["job_id"])["summary"] == {"applied": 0, "errors": 3}
    for index, track in enumerate(ordered, start=5):
        updated = env.db.get_track(track.id)
        assert updated.track_no == index
        assert updated.disc_no == (disc_no or track.disc_no)
        assert (updated.track_total, updated.disc_total) == (12, 2)
        assert updated.title == track.title
        assert updated.artist == track.artist
        assert updated.path == track.path
        current = dict(mutagen.File(track.path, easy=True))
        assert current.pop("tracknumber") == [f"{index}/12"]
        original = dict(tags_before[track.id])
        original.pop("tracknumber")
        if disc_no is not None:
            assert current.pop("discnumber") == ["2/2"]
            original.pop("discnumber")
        assert current == original
        assert read_cover(track.path, include_folder=False) == covers[track.id]
    assert Path(tracks[3].path).read_bytes() == before[tracks[3].id]


@pytest.mark.parametrize(("start_no", "disc_no", "message"), [
    (0, None, "Track number"),
    (True, None, "Track number"),
    (1.5, None, "Track number"),
    ("١", None, "Track number"),
    ("", None, "Track number"),
    (9999, None, "sequence"),
    (1, 0, "Disc number"),
    (1, 10000, "Disc number"),
    (1, False, "Disc number"),
    (1, "1/2", "Disc number"),
])
def test_album_numbering_rejects_invalid_sequence_before_saving_reviews(
    lib_env, start_no, disc_no, message,
):
    env = lib_env
    for index in range(2):
        make_mp3(env.library / f"{index}.mp3", tags={"title": f"Song {index}"})
    Scanner(env.config, env.db, workers=1).scan()
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/numbering/preview", json={
            "track_ids": [track.id for track in env.db.all_tracks()],
            "start_no": start_no, "disc_no": disc_no,
        })
        assert response.status_code == 400
        assert message in response.json()["detail"]
        assert env.db.query_one("SELECT COUNT(*) AS n FROM metadata_reviews")["n"] == 0
        assert client.get("/api/jobs").json() == []


def test_album_numbering_keeps_slots_for_unavailable_or_failed_files(lib_env, monkeypatch):
    env = lib_env
    for index in range(4):
        make_mp3(env.library / f"{index}.mp3", tags={
            "title": f"Song {index}", "album": "Album", "tracknumber": index + 1,
        })
    Scanner(env.config, env.db, workers=1).scan()
    tracks = env.db.all_tracks()
    Path(tracks[1].path).unlink()
    write = review_module.write_audio_tags

    def deny_one(path, values, **kwargs):
        if str(path) == tracks[2].path:
            raise PermissionError("Read-only music folder")
        return write(path, values, **kwargs)

    monkeypatch.setattr(review_module, "write_audio_tags", deny_one)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/metadata/numbering/preview", json={
            "track_ids": [track.id for track in tracks], "start_no": 4,
        })
        assert response.status_code == 200
        preview = response.json()
        assert len(preview["errors"]) == 1
        assert preview["errors"][0]["track_id"] == tracks[1].id
        assert [review["candidates"][0]["fields"]["track_no"] for review in preview["reviews"]] == [
            4, 6, 7,
        ]
        changed = Path(tracks[0].path)
        changed.write_bytes(changed.read_bytes() + b"external edit")
        response = client.post("/api/metadata/numbering/apply", json={"reviews": [
            {"review_id": review["review_id"], "candidate_index": 0, "fields": ["track_no"]}
            for review in preview["reviews"]
        ]})
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"] == {"applied": 1, "errors": 2}
        errors = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"]
        assert "changed since lookup" in errors[0]["error"]
        assert "Read-only" in errors[1]["error"]
    assert env.db.get_track(tracks[0].id).track_no == 1
    assert env.db.get_track(tracks[2].id).track_no == 3
    assert env.db.get_track(tracks[3].id).track_no == 7


def test_album_numbering_limits_and_numbering_only_apply(lib_env):
    env = lib_env
    make_mp3(env.library / "song.mp3", tags={"title": "Song"})
    Scanner(env.config, env.db, workers=1).scan()
    track = env.db.all_tracks()[0]
    with TestClient(create_app(env.config)) as client:
        for ids in ([], [track.id, track.id], list(range(201))):
            assert client.post("/api/metadata/numbering/preview", json={
                "track_ids": ids,
            }).status_code == 400
        preview = client.post("/api/metadata/numbering/preview", json={
            "track_ids": [track.id], "start_no": 9999,
        }).json()
        review_id = preview["reviews"][0]["review_id"]
        assert preview["reviews"][0]["candidates"][0]["fields"] == {"track_no": 9999}
        for fields in ([], ["title"], ["album"]):
            assert client.post("/api/metadata/numbering/apply", json={"reviews": [{
                "review_id": review_id, "candidate_index": 0, "fields": fields,
            }]}).status_code == 400
        batch = {"reviews": [
            {"review_id": index, "candidate_index": 0, "fields": ["track_no"]}
            for index in range(200)
        ]}
        response = client.post("/api/metadata/numbering/apply", json=batch)
        assert response.status_code == 200
        # Only the one real review is available; its value came from the saved preview.
        assert wait_job(client, response.json()["job_id"])["summary"] == {"applied": 1, "errors": 199}
        assert env.db.get_track(track.id).track_no == 9999
        assert client.post("/api/metadata/apply-bulk", json=batch).status_code == 400
        assert client.post("/api/metadata/numbering/apply", json={
            "reviews": batch["reviews"] + [batch["reviews"][0]],
        }).status_code == 400


def test_itunes_results_are_used_when_musicbrainz_fails(monkeypatch):
    def fail(**kwargs):
        raise musicbrainzngs.NetworkError("connection timed out")

    monkeypatch.setattr("musiclib.metadata_review.search_recordings", fail)
    monkeypatch.setattr(
        "musiclib.metadata_review.search_itunes",
        lambda *args, **kwargs: [{
            "source": "iTunes", "confidence": 80, "cover_url": "https://example.test/a.jpg",
            "cover_fallback": None, "fields": {
                "title": "Correct Song", "artist": "Correct Artist", "album": "Correct Album",
                "album_artist": "Correct Artist", "year": 2004, "track_no": 1, "disc_no": 1,
                "genre": "Rock", "mbid_track": None, "mbid_release": None,
            },
        }],
    )
    warnings = []
    results = lookup_track(Track(title="Correct Song", artist="Correct Artist"), warnings=warnings)
    assert results[0]["source"] == "iTunes"
    assert results[0]["fields"]["title"] == "Correct Song"
    assert "connection timed out" in warnings[0]


def test_search_itunes_parses_artwork_and_reports_network_failure(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [_itunes_song(), "skip", {"trackName": ""}]}

    monkeypatch.setattr("musiclib.metadata_review.httpx.get", lambda *args, **kwargs: FakeResponse())
    results = search_itunes(
        "Correct Song", "Correct Artist", Track(title="Correct Song", artist="Correct Artist")
    )
    assert results[0]["cover_url"] == "https://example.test/cover/300x300bb.jpg"
    assert results[0]["fields"]["genre"] == "Rock"
    assert results[0]["fields"]["year"] == 2004

    def boom(*args, **kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("musiclib.metadata_review.httpx.get", boom)
    with pytest.raises(RuntimeError, match="iTunes lookup failed: slow"):
        search_itunes("Song", "Artist", Track(title="Song"))
