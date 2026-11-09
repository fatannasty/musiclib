
import musicbrainzngs
import pytest
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
    expected = {"limit": 10}
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
    assert calls == [{"limit": 10, "recording": "Correct Song"}]


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
