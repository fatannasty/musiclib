
from conftest import make_mp3
from fastapi.testclient import TestClient

from musiclib.metadata_review import MetadataReview, lookup_track
from musiclib.models import Track
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


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
