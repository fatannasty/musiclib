import threading
from pathlib import Path

import httpx
import musicbrainzngs
import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.metadata_review import MetadataReview, lookup_track, search_itunes
from musiclib.models import Track
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


@pytest.fixture(autouse=True)
def _quiet_itunes(monkeypatch):
    def offline(*args, **kwargs):
        raise httpx.TimeoutException("itunes disabled in tests")
    monkeypatch.setattr("musiclib.metadata_review.httpx.get", offline)


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
        "musiclib.metadata_review.musicbrainzngs.search_recordings",
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
        "musiclib.metadata_review.musicbrainzngs.search_recordings", lambda **kwargs: None
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
        "musiclib.metadata_review.musicbrainzngs.search_recordings",
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
        "musiclib.metadata_review.musicbrainzngs.search_recordings",
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
        "musiclib.metadata_review.musicbrainzngs.search_recordings",
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
        "musiclib.metadata_review.musicbrainzngs.search_recordings",
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
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
        musicbrainzngs, "search_recordings", lambda **kwargs: _musicbrainz_result()
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


def test_bulk_provider_failure_continues_and_holds_library_lock(lib_env, monkeypatch):
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

    monkeypatch.setattr(musicbrainzngs, "search_recordings", search)
    with TestClient(create_app(env.config)) as client:
        try:
            response = client.post("/api/metadata/lookup-bulk", json={
                "track_ids": [track.id for track in env.db.all_tracks()],
            })
            assert response.status_code == 200
            job_id = response.json()["job_id"]
            assert entered.wait(5)
            assert client.post("/api/metadata/lookup", json={"track_id": 1}).status_code == 409
            assert client.post("/api/metadata/apply-bulk", json={"reviews": [{
                "review_id": 1, "candidate_index": 0, "fields": ["title"],
            }]}).status_code == 409
        finally:
            finish.set()
        assert wait_job(client, job_id)["summary"] == {"reviews": 1, "errors": 1}
        error = client.get(f"/api/jobs/{job_id}/results/errors").json()["items"][0]
        assert "provider unavailable" in error["error"]


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
        "musiclib.metadata_review.musicbrainzngs.search_recordings",
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
        "musiclib.metadata_review.musicbrainzngs.search_releases",
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
    monkeypatch.setattr("musiclib.metadata_review.musicbrainzngs.search_releases", lambda **kwargs: {"release-list": []})
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


def test_itunes_results_are_used_when_musicbrainz_fails(monkeypatch):
    def fail(**kwargs):
        raise musicbrainzngs.NetworkError("connection timed out")

    monkeypatch.setattr("musiclib.metadata_review.musicbrainzngs.search_recordings", fail)
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
    results = lookup_track(Track(title="Correct Song", artist="Correct Artist"))
    assert results[0]["source"] == "iTunes"
    assert results[0]["fields"]["title"] == "Correct Song"


def test_search_itunes_parses_artwork_and_ignores_bad_payloads(monkeypatch):
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
    assert search_itunes("Song", "Artist", Track(title="Song")) == []
