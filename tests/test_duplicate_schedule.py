import time

import pytest
from conftest import make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.server.app import create_app
from musiclib.server.duplicate_schedule import DuplicateSchedule
from musiclib.server.jobs import JobRegistry


def test_schedule_is_opt_in_persistent_and_only_discovers(lib_env):
    jobs = JobRegistry(lib_env.db)
    calls = []
    schedule = DuplicateSchedule(
        lib_env.config, lib_env.db, jobs, lambda job: calls.append(job["origin"]) or {"groups": []},
    )
    assert schedule.status(now=100)["next_run"] is None
    schedule.tick(now=1000000)
    assert calls == []
    lib_env.config.duplicates["schedule_hours"] = 24
    assert schedule.status(now=100)["next_run"] == 86500
    restarted = DuplicateSchedule(lib_env.config, lib_env.db, jobs, schedule.discover)
    assert restarted.status(now=200)["next_run"] == 86500
    restarted.tick(now=86499)
    assert calls == []
    restarted.tick(now=86500)
    jobs.close()
    assert calls == ["schedule"]
    assert jobs.list()[0]["kind"] == "duplicates"
    assert jobs.list()[0]["status"] == "done"
    assert restarted.status(now=86500)["next_run"] == 172900
    assert lib_env.db.query("SELECT * FROM deletions") == []
    lib_env.config.duplicates["schedule_hours"] = 0
    restarted.tick(now=999999)
    assert calls == ["schedule"]


def test_busy_schedule_defers_without_losing_due_run(lib_env):
    jobs = JobRegistry(lib_env.db)
    lib_env.config.duplicates["schedule_hours"] = 24
    schedule = DuplicateSchedule(lib_env.config, lib_env.db, jobs, lambda job: {})
    due = schedule.status(now=0)["next_run"]
    jobs._lease.acquire()
    try:
        schedule.tick(now=due)
        assert jobs.list() == []
        assert schedule.status(now=due)["next_run"] == due
    finally:
        jobs._lease.release()
    schedule.tick(now=due + 30)
    jobs.close()
    assert len(jobs.list()) == 1


@pytest.mark.parametrize("settings", [
    {"schedule_hours": -1}, {"schedule_hours": 1}, {"strategy": "anything"},
    {"duration_tolerance": -1}, {"duration_tolerance": 1000},
])
def test_invalid_discovery_settings_never_enable_automation(lib_env, settings):
    with TestClient(create_app(lib_env.config)) as client:
        assert client.put("/api/settings", json={"duplicates": settings}).status_code == 400
        assert client.get("/api/dupes/schedule").json()["hours"] == 0


def test_failed_settings_save_does_not_enable_automation(lib_env, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Read-only config folder")
    monkeypatch.setattr("musiclib.config.Config.save", fail)
    with TestClient(create_app(lib_env.config)) as client:
        response = client.put("/api/settings", json={
            "duplicates": {"schedule_hours": 24, "schedule_fingerprint": False},
        })
        assert response.status_code == 500
        assert client.get("/api/dupes/schedule").json()["hours"] == 0


def test_enable_schedule_waits_an_interval_and_reports_next_run(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        now = time.time()
        response = client.put("/api/settings", json={
            "duplicates": {"schedule_hours": 168, "schedule_fingerprint": False},
        })
        assert response.status_code == 200
        status = client.get("/api/dupes/schedule").json()
        assert status["hours"] == 168
        assert status["next_run"] >= now + 168 * 3600
        assert client.get("/api/jobs").json() == []


def test_scheduled_discovery_scans_new_files_and_never_trashes_them(lib_env, monkeypatch):
    paths = [lib_env.library / f"{index}.mp3" for index in range(2)]
    for path in paths:
        make_mp3(path, tags={"artist": "Artist", "title": "Same Song", "album": path.stem})
    lib_env.config.duplicates.update(schedule_hours=24, schedule_fingerprint=False)
    original_start = DuplicateSchedule.start

    def run_due(self):
        original_start(self)
        self.tick(now=self.status()["next_run"])
    monkeypatch.setattr(DuplicateSchedule, "start", run_due)
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.get("/api/jobs").json()[0]["id"]
        job = wait_job(client, job_id)
        assert job["origin"] == "schedule"
        assert job["status"] == "done"
        assert job["summary"]["groups"] == 1
        assert client.get("/api/stats").json()["tracks"] == 2
        assert all(path.is_file() for path in paths)
        assert lib_env.db.query("SELECT * FROM deletions") == []
