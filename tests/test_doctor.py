import os

from conftest import ALBUM_A, make_flac, make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.doctor import Doctor
from musiclib.fsops import trash_file
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


def test_missing_selected_file_is_skipped_not_claimed_as_trashed(lib_env):
    path = make_mp3(lib_env.library / "gone.mp3", tags={"title": "Gone"})
    Scanner(lib_env.config, lib_env.db, workers=1).scan()
    track = lib_env.db.all_tracks()[0]
    os.unlink(path)
    outcomes = []
    report = Doctor(lib_env.config, lib_env.db).trash_tracks([track.id], outcome=outcomes.append)
    assert report.trashed == 0
    assert report.skipped == 1
    assert outcomes[-1]["status"] == "skipped"
    assert "already missing" in outcomes[-1]["reason"]


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


def test_doctor_trash_continues_after_oserror(lib_env, monkeypatch):
    env = lib_env
    (env.library / "a.flac").write_bytes(b"not a flac file at all")
    (env.library / "b.flac").write_bytes(b"also not a flac file")
    Scanner(env.config, env.db).scan()
    seen = {"n": 0}

    def flaky(path, trash_dir):
        seen["n"] += 1
        if seen["n"] == 1:
            raise OSError("permission denied")
        return trash_file(path, trash_dir)

    monkeypatch.setattr("musiclib.doctor.trash_file", flaky)
    report = Doctor(env.config, env.db).check(deep=False, apply_actions=True)
    assert report.failed == 1
    assert report.trashed == 1
    assert len(report.failures) == 1
    assert "permission denied" in report.failures[0]["error"]
    remaining = [track for track in env.db.all_tracks() if track.status != "deleted"]
    assert len(remaining) == 1


def test_selected_trash_reports_per_file_errors(lib_env, monkeypatch):
    env = lib_env
    (env.library / "keep.flac").write_bytes(b"not a flac file at all")
    (env.library / "drop.flac").write_bytes(b"also not a flac file")
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()

    def flaky(path, trash_dir):
        if path.endswith("keep.flac"):
            raise OSError("permission denied")
        return trash_file(path, trash_dir)

    monkeypatch.setattr("musiclib.doctor.trash_file", flaky)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/issues/trash", json={"track_ids": [track.id for track in tracks]})
        assert response.status_code == 200
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "done"
        assert job["summary"]["trashed"] == 1
        assert job["summary"]["failed"] == 1
        failures = client.get(f"/api/jobs/{job['id']}/results/failures").json()["items"]
        assert "permission denied" in failures[0]["error"]
        assert client.post("/api/issues/trash", json={"track_ids": []}).status_code == 400
        assert client.post("/api/issues/trash", json={"track_ids": [1, 1]}).status_code == 400
    deleted = [track for track in tracks if env.db.get_track(track.id).status == "deleted"]
    blocked = [track for track in tracks if env.db.get_track(track.id).status != "deleted"]
    assert len(deleted) == 1
    assert len(blocked) == 1
    assert os.path.exists(blocked[0].path)
