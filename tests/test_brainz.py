import json

from conftest import ALBUM_A, make_flac

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
