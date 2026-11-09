import os

import pytest
from conftest import ALBUM_A, make_flac

from musiclib.duplicates import DuplicateFinder, normalize, score_track
from musiclib.models import Issue, Track
from musiclib.scanner import Scanner


def _track(**kwargs):
    defaults = dict(path="/lib/a.flac", format="flac", bitrate=900, sample_rate=44100,
                    bit_depth=16, title="T", album="AL", artist="AR", album_artist="AR",
                    duration=180.0, file_hash="h1")
    defaults.update(kwargs)
    return Track(**defaults)


def test_normalize_strips_punctuation_and_feat():
    assert normalize("Hello, World!") == "hello world"
    assert normalize("Song (feat. Someone)") == "song"
    assert normalize("  A   B  ") == "a b"


def test_score_prefers_lossless():
    flac = _track(format="flac", bit_depth=16)
    mp3_320 = _track(path="/lib/a.mp3", format="mp3", bitrate=320, file_hash="h2")
    mp3_128 = _track(path="/lib/b.mp3", format="mp3", bitrate=128, file_hash="h3")
    assert score_track(flac) > score_track(mp3_320) > score_track(mp3_128)


def test_score_penalizes_bad_issues():
    clean = _track()
    broken = _track(path="/lib/bad.flac", file_hash="h2")
    broken.issues = [Issue("decode_error", "x", "bad")]
    assert score_track(broken) < score_track(clean)


def test_find_groups_exact_hash_and_tags(lib_env, mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    finder = DuplicateFinder(env.config, env.db)
    groups = finder.find()
    assert len(groups) == 1
    group = groups[0]
    member_paths = {m.track.path for m in group.members}
    assert any("First Song (copy).flac" in p for p in member_paths)
    assert any("First Song.mp3" in p for p in member_paths)
    assert any("01 - First Song.flac" in p for p in member_paths)
    assert group.keep.track.format == "flac"
    losers = [m for m in group.members if m.action == "loser"]
    assert all(m.track.format != "flac" or "(copy)" in m.track.path for m in losers)


def test_find_ignores_different_titles(lib_env):
    env = lib_env
    make_flac(env.library / "a.flac", tags={**ALBUM_A, "TITLE": "Song X", "TRACKNUMBER": "1"})
    make_flac(env.library / "b.flac", tags={**ALBUM_A, "TITLE": "Song Y", "TRACKNUMBER": "2"})
    Scanner(env.config, env.db).scan()
    groups = DuplicateFinder(env.config, env.db).find()
    assert groups == []


def test_duration_tolerance_splits_groups(lib_env):
    env = lib_env
    make_flac(env.library / "a.flac", tags={**ALBUM_A, "TITLE": "Song X"}, duration=180)
    make_flac(env.library / "b.flac", tags={**ALBUM_A, "TITLE": "Song X"}, duration=400)
    Scanner(env.config, env.db).scan()
    env.config.duplicates["duration_tolerance"] = 5
    groups = DuplicateFinder(env.config, env.db).find()
    assert groups == []


def test_apply_trashes_losers(lib_env, mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    finder = DuplicateFinder(env.config, env.db)
    groups = finder.find()
    keep_path = groups[0].keep.track.path
    loser_paths = [m.track.path for m in groups[0].members if m.action == "loser"]
    report = finder.apply(groups)
    assert report.trashed == len(loser_paths)
    assert report.deleted == 0
    assert os.path.exists(keep_path)
    trashed_names = set()
    trash_root = os.path.join(env.config.trash_dir, os.listdir(env.config.trash_dir)[0])
    for name in os.listdir(trash_root):
        trashed_names.add(name)
    for path in loser_paths:
        assert not os.path.exists(path)
        assert os.path.basename(path) in trashed_names
    rows = env.db.query("SELECT * FROM deletions WHERE reason LIKE 'duplicate%'")
    assert len(rows) == len(loser_paths)
    trashed_db = env.db.all_tracks(status="deleted")
    assert len(trashed_db) == len(loser_paths)


def test_apply_respects_keep_override(lib_env, mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    finder = DuplicateFinder(env.config, env.db)
    groups = finder.find()
    mp3_member = next(m for m in groups[0].members if m.track.format == "mp3")
    finder.apply(groups, keep_ids={groups[0].group_id: mp3_member.track.id})
    assert os.path.exists(mp3_member.track.path)
    keep_row = env.db.query_one(
        "SELECT path FROM tracks WHERE id=?", (groups[0].keep.track.id,)
    )
    assert not os.path.exists(keep_row["path"])


def test_manual_group_trashes_selected_tracks(lib_env, mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    flac = next(track for track in tracks if track.path.endswith("First Song.flac"))
    mp3 = next(track for track in tracks if track.path.endswith("First Song.mp3"))
    finder = DuplicateFinder(env.config, env.db)
    group = finder.manual_group([flac.id, mp3.id], keep_id=mp3.id)
    report = finder.apply([group])
    assert report.trashed == 1
    assert os.path.exists(mp3.path)
    assert not os.path.exists(flac.path)


def test_invalid_keeper_never_deletes_anything(mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    finder = DuplicateFinder(env.config, env.db)
    groups = finder.find()
    paths = [member.track.path for member in groups[0].members]
    with pytest.raises(ValueError, match="keeper"):
        finder.apply(groups, keep_ids={groups[0].group_id: -1})
    assert all(os.path.exists(path) for path in paths)


def test_fingerprint_comparison_crosses_old_bucket_boundaries(lib_env):
    fp = ",".join(["123456"] * 16)
    for index, duration in enumerate((177.9, 178.1)):
        lib_env.db.upsert_track(Track(
            path=f"/lib/{index}.flac", duration=duration,
            title=f"Title {index}", album=f"Album {index}", fingerprint=fp,
        ))
    groups = DuplicateFinder(lib_env.config, lib_env.db).find(fingerprint_pass=True)
    assert len(groups) == 1


def test_fingerprint_comparison_does_not_skip_large_duration_buckets(lib_env):
    fp = ",".join(["123456"] * 16)
    for index in range(61):
        lib_env.db.upsert_track(Track(
            path=f"/lib/{index}.flac", duration=180,
            title=f"Title {index}", album=f"Album {index}", fingerprint=fp,
        ))
    groups = DuplicateFinder(lib_env.config, lib_env.db).find(fingerprint_pass=True)
    assert len(groups) == 1
    assert len(groups[0].members) == 61
