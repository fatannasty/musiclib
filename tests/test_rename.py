import os

from conftest import ALBUM_A, make_flac, make_mp3

from musiclib.rename import Renamer, sanitize_component
from musiclib.scanner import Scanner


def test_sanitize_component():
    assert sanitize_component("AC/DC: Back In Black?") == "AC_DC_ Back In Black"
    assert sanitize_component("trailing dots... ") == "trailing dots"
    assert sanitize_component("") == "_"
    assert len(sanitize_component("x" * 500).encode("utf-8")) <= 120


def test_target_path_formats(lib_env):
    renamer = Renamer(lib_env.config, lib_env.db)
    from musiclib.models import Track

    track = Track(
        path="/tmp/whatever.flac", album_artist="Artist One", album="Album A",
        title="First Song", track_no=1, year=2001, format="flac",
    )
    target = renamer.target_path(track)
    assert target == os.path.join(
        lib_env.config.library, "Artist One", "Album A (2001)", "01 - First Song.flac"
    )
    track.disc_total = 2
    track.disc_no = 2
    target = renamer.target_path(track)
    assert os.sep + "CD2" + os.sep in target


def test_target_path_sanitizes_dangerous_names(lib_env):
    renamer = Renamer(lib_env.config, lib_env.db)
    from musiclib.models import Track

    track = Track(
        path="/tmp/x.mp3", album_artist='AC/DC', album='Back "In" Black',
        title='Hell\'s Bells: Part 1?', track_no=9, year=1980, format="mp3",
    )
    target = renamer.target_path(track)
    assert "/" not in target.split(os.sep)[-1]
    assert "?" not in target and '"' not in target


def test_plan_and_apply_organizes_messy_library(lib_env):
    env = lib_env
    make_mp3(
        env.library / "messy dir" / "track1.mp3",
        tags={"title": "First Song", "artist": "Artist One", "album": "Album A",
              "albumartist": "Artist One", "tracknumber": "1", "date": "2001"},
    )
    make_flac(
        env.library / "another-mess" / "02 Second.flac",
        tags={**ALBUM_A, "TITLE": "Second Song", "TRACKNUMBER": "2"},
    )
    Scanner(env.config, env.db).scan()
    renamer = Renamer(env.config, env.db)
    report = renamer.plan()
    assert len(report.moves) == 2
    done = renamer.apply(report)
    assert done.moved == 2
    expected1 = os.path.join(env.config.library, "Artist One", "Album A (2001)", "01 - First Song.mp3")
    expected2 = os.path.join(env.config.library, "Artist One", "Album A (2001)", "02 - Second Song.flac")
    assert os.path.exists(expected1)
    assert os.path.exists(expected2)
    row = env.db.query_one("SELECT path FROM tracks WHERE title='First Song'")
    assert row["path"] == expected1


def test_rename_is_idempotent(lib_env):
    env = lib_env
    make_flac(
        env.library / "mess" / "one.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"},
    )
    Scanner(env.config, env.db).scan()
    renamer = Renamer(env.config, env.db)
    renamer.apply(renamer.plan())
    second = renamer.plan()
    assert second.moves == []
    assert second.already_ok == 1


def test_rename_collision_gets_suffix(lib_env):
    env = lib_env
    make_flac(
        env.library / "a" / "one.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"}, duration=100,
    )
    make_flac(
        env.library / "b" / "two.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"}, duration=200,
    )
    Scanner(env.config, env.db).scan()
    renamer = Renamer(env.config, env.db)
    report = renamer.apply(renamer.plan())
    assert report.moved == 2
    assert report.errors == []
    files = os.listdir(os.path.join(env.config.library, "Artist One", "Album A (2001)"))
    assert "01 - First Song.flac" in files
    assert "01 - First Song (2).flac" in files


def test_rename_skips_untagged(lib_env):
    env = lib_env
    from conftest import make_wav

    make_wav(env.library / "nope" / "mystery.wav")
    Scanner(env.config, env.db).scan()
    renamer = Renamer(env.config, env.db)
    report = renamer.plan()
    assert report.moves == []
    assert report.skipped and "tags" in report.skipped[0][1]


def test_rename_onto_path_of_trashed_duplicate(lib_env):
    env = lib_env
    make_flac(
        env.library / "keep" / "backup-copy.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"}, duration=12.0,
    )
    make_flac(
        env.library / "messy" / "01 - First Song.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"}, duration=12.0,
    )
    Scanner(env.config, env.db).scan()
    from musiclib.duplicates import DuplicateFinder

    finder = DuplicateFinder(env.config, env.db)
    groups = finder.find()
    assert len(groups) == 1
    finder.apply(groups)
    renamer = Renamer(env.config, env.db)
    report = renamer.apply(renamer.plan())
    assert report.errors == []
    target = os.path.join(
        env.config.library, "Artist One", "Album A (2001)", "01 - First Song.flac"
    )
    assert os.path.exists(target)
    rows = env.db.query("SELECT * FROM tracks WHERE path=?", (target,))
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
