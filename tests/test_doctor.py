import os

from conftest import ALBUM_A, make_flac, make_mp3

from musiclib.doctor import Doctor
from musiclib.scanner import Scanner


def test_doctor_flags_low_bitrate_and_short_files(lib_env):
    env = lib_env
    make_mp3(
        env.library / "a" / "low.mp3",
        tags={**ALBUM_A, "TITLE": "Low", "TRACKNUMBER": "1"}, bitrate=64,
    )
    make_mp3(
        env.library / "a" / "short.mp3",
        tags={**ALBUM_A, "TITLE": "Short", "TRACKNUMBER": "2"}, bitrate=320, seconds=3,
    )
    make_mp3(
        env.library / "a" / "good.mp3",
        tags={**ALBUM_A, "TITLE": "Good", "TRACKNUMBER": "3"}, bitrate=320, seconds=15,
    )
    Scanner(env.config, env.db).scan()
    doctor = Doctor(env.config, env.db)
    report = doctor.check(deep=False)
    assert report.bad == 0
    assert report.suspect == 2
    assert {row["path"] for row in report.results} == {
        str(env.library / "a" / "low.mp3"),
        str(env.library / "a" / "short.mp3"),
    }
    by_title = {t.title: t for t in env.db.all_tracks()}
    assert by_title["Low"].status == "suspect"
    assert any(i.kind == "low_bitrate" for i in by_title["Low"].issues)
    assert by_title["Short"].status == "suspect"
    assert any(i.kind == "short_file" for i in by_title["Short"].issues)
    assert by_title["Good"].status == "ok"


def test_doctor_marks_unparseable_bad_and_trashes(lib_env):
    env = lib_env
    (env.library / "junk.flac").write_bytes(b"not a flac file at all")
    Scanner(env.config, env.db).scan()
    doctor = Doctor(env.config, env.db)
    report = doctor.check(deep=False, apply_actions=True)
    assert report.bad == 1
    assert report.trashed == 1
    assert not os.path.exists(env.library / "junk.flac")
    assert env.db.query_one("SELECT COUNT(*) AS n FROM deletions")["n"] == 1
    assert env.db.all_tracks(status="bad") == []


def test_doctor_deep_skipped_without_ffmpeg(lib_env, monkeypatch):
    env = lib_env
    monkeypatch.setattr("musiclib.doctor.find_binary", lambda name: None)
    make_flac(env.library / "a" / "ok.flac", tags={**ALBUM_A, "TITLE": "Ok"})
    Scanner(env.config, env.db).scan()
    doctor = Doctor(env.config, env.db)
    report = doctor.check(deep=True)
    assert report.checked == 1
    assert report.bad == 0
    assert any("ffmpeg" in ",".join(r["issues"]) for r in report.results)
    assert env.db.all_tracks()[0].status == "ok"
