import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from musiclib.doctor import Doctor
from musiclib.models import Issue, Track
from musiclib.scanner import Scanner, ScanReport
from musiclib.server.app import create_app
from musiclib.server.jobs import JobRegistry


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(.01)
    pytest.fail("job did not finish")


def test_job_progress_survives_refresh_and_reports_real_counts(lib_env, monkeypatch):
    entered, finish = threading.Event(), threading.Event()

    def scan(self, **kwargs):
        kwargs["progress_state"](651, 1000, "/music/current.flac")
        entered.set()
        assert finish.wait(5)
        kwargs["progress_state"](1000, 1000, "/music/last.flac")
        return ScanReport(scanned=1000, added=1000)

    monkeypatch.setattr("musiclib.server.app.Scanner.scan", scan)
    with TestClient(create_app(lib_env.config)) as client:
        try:
            job_id = client.post("/api/scan", json={}).json()["job_id"]
            assert entered.wait(5)
            job = client.get("/api/jobs").json()[0]
            assert job["completed"] == 651
            assert job["total"] == 1000
            assert job["current"] == "/music/current.flac"
            assert client.get("/").status_code == 200
            assert client.get(f"/api/jobs/{job_id}").json()["completed"] == 651
            assert client.post("/api/tasks/doctor", json={}).status_code == 409
            assert client.post("/api/dupes/manual", json={"track_ids": [1, 2], "keep_id": 1}).status_code == 409
            assert client.put("/api/settings", json={}).status_code == 409
        finally:
            finish.set()
        assert wait_job(client, job_id)["summary"]["added"] == 1000
    with TestClient(create_app(lib_env.config)) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "done"


def test_interrupted_jobs_are_reported(lib_env):
    registry = JobRegistry(lib_env.db)
    registry._save({
        "id": "interrupted", "kind": "scan", "status": "running",
        "started": time.time(), "finished": None,
    })
    restarted = JobRegistry(lib_env.db)
    assert restarted.get("interrupted")["status"] == "error"
    assert "restarted" in restarted.get("interrupted")["error"]


def test_failure_is_visible_and_releases_library_lock(lib_env, monkeypatch):
    def fail(self, **kwargs):
        raise OSError("share unavailable")

    monkeypatch.setattr("musiclib.server.app.Scanner.scan", fail)
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/scan", json={}).json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "error"
        assert "share unavailable" in job["error"]
        assert client.post("/api/tasks/rename", json={}).status_code == 200


@pytest.mark.parametrize("outcome", ["done", "error", "cancelled"])
def test_album_finalization_finishes_before_job_status_and_write_lane_release(lib_env, outcome):
    entered, finish = threading.Event(), threading.Event()

    def finalize():
        entered.set()
        assert finish.wait(5)
        Scanner(lib_env.config, lib_env.db).rebuild_albums()

    registry = JobRegistry(lib_env.db, after_write=finalize)

    def run(job):
        lib_env.db.upsert_track(Track(path="/music/song", album="New", album_artist="Artist"))
        if outcome == "error":
            raise RuntimeError("Write interrupted")
        if outcome == "cancelled":
            raise CancelledError()
        return {}

    try:
        job_id = registry.start("test", run)
        assert entered.wait(5)
        assert registry.get(job_id)["status"] == "running"
        with pytest.raises(HTTPException) as error:
            registry.start("other-write", lambda job: {})
        assert error.value.status_code == 409
    finally:
        finish.set()
        registry.close()
    assert registry.get(job_id)["status"] == outcome
    assert [(a.album, a.have_tracks) for a in lib_env.db.albums_all()] == [("New", 1)]


def test_album_finalization_failure_is_visible_and_releases_write_lane(lib_env):
    def fail():
        raise OSError("Album refresh failed")

    registry = JobRegistry(lib_env.db, after_write=fail)
    job_id = registry.start("test", lambda job: {})
    registry.close()
    job = registry.get(job_id)
    assert job["status"] == "error"
    assert "Album refresh failed" in job["error"]
    with pytest.raises(OSError, match="Album refresh failed"):
        registry.operation(lambda: {})()
    registry.after_write = None
    assert registry.operation(lambda: "available")() == "available"


def test_search_and_write_lanes_are_independent_and_bounded(lib_env):
    registry = JobRegistry(lib_env.db)
    searching, writing = threading.Event(), threading.Event()
    check_guard, checked, finish = threading.Event(), threading.Event(), threading.Event()

    @registry.operation
    def guarded_write():
        pytest.fail("A search job must not bypass an active write lock")

    def search(job):
        searching.set()
        assert check_guard.wait(5)
        with pytest.raises(HTTPException) as error:
            guarded_write()
        assert error.value.status_code == 409
        checked.set()
        assert finish.wait(5)
        return {}

    def write(job):
        writing.set()
        assert finish.wait(5)
        return {}

    try:
        search_id = registry.start("metadata-lookup", search, read_only=True)
        write_id = registry.start("scan", write)
        assert searching.wait(5) and writing.wait(5)
        active = [job for job in registry.list() if job["status"] == "running"]
        assert len(active) == 2
        assert registry.get(search_id)["read_only"] is True
        assert registry.get(write_id)["read_only"] is False
        for read_only in (True, False):
            with pytest.raises(HTTPException) as error:
                registry.start("extra", lambda job: {}, read_only=read_only)
            assert error.value.status_code == 409
        check_guard.set()
        assert checked.wait(5)
    finally:
        check_guard.set()
        finish.set()
        registry.close()
    assert registry.get(search_id)["status"] == "done"
    assert registry.get(write_id)["status"] == "done"
    assert registry.lookup_operation(lambda: "search available")() == "search available"
    assert registry.operation(lambda: "write available")() == "write available"


@pytest.mark.parametrize("read_only", [True, False])
def test_worker_start_failure_releases_its_lane(lib_env, monkeypatch, read_only):
    registry = JobRegistry(lib_env.db)

    def fail_start(self):
        raise RuntimeError("Cannot create worker")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="Cannot create worker"):
        registry.start("test", lambda job: {}, read_only=read_only)
    assert registry.list()[0]["status"] == "error"
    guard = registry.lookup_operation if read_only else registry.operation
    assert guard(lambda: "available")() == "available"


@pytest.mark.parametrize("finish", ["stop", "error"])
def test_file_outcomes_survive_partial_jobs_and_restart(lib_env, finish):
    registry = JobRegistry(lib_env.db)

    def run(job):
        registry.plan([{"track_id": index, "path": f"/music/{index}.mp3"} for index in range(3)])
        registry.outcome({"track_id": 0, "status": "completed", "reason": "Saved reviewed tags."})
        registry.outcome({"track_id": 1, "status": "failed", "error": "Read-only folder"})
        if finish == "stop":
            registry.cancel(job["id"])
            registry.update("Next file", 2, 3)
        raise OSError("Share disconnected")
    job_id = registry.start("metadata-apply", run)
    registry.close()
    job = registry.get(job_id)
    assert job["outcomes"] == {"completed": 1, "failed": 1, "stopped": 1}
    assert [item["status"] for item in registry.items(job_id)] == ["completed", "failed", "stopped"]
    restarted = JobRegistry(lib_env.db)
    assert restarted.items(job_id)[1]["error"] == "Read-only folder"
    assert restarted.items(job_id)[2]["path"] == "/music/2.mp3"
    assert restarted.get(job_id)["status"] == ("cancelled" if finish == "stop" else "error")


def test_unreported_planned_work_is_not_reported_as_success_or_a_harmless_skip(lib_env):
    registry = JobRegistry(lib_env.db)

    def run(job):
        registry.plan([{"track_id": 1, "path": "/music/uncertain.mp3"}])
        return {}
    job_id = registry.start("test", run)
    registry.close()
    assert registry.get(job_id)["outcomes"] == {"stopped": 1}
    assert "Inspect" in registry.items(job_id)[0]["reason"]


def test_reviewed_trash_reports_paths_and_retry_excludes_successes(lib_env, monkeypatch):
    from conftest import make_mp3

    from musiclib.trash_review import TrashReview
    for index in range(2):
        make_mp3(lib_env.library / f"{index}.mp3", tags={"title": f"Song {index}"})
    Scanner(lib_env.config, lib_env.db, workers=1).scan()
    tracks = lib_env.db.all_tracks()
    move = TrashReview._move

    def fail_second(self, snapshot):
        if snapshot["track_id"] == tracks[1].id:
            raise PermissionError("Read-only music folder")
        return move(self, snapshot)
    monkeypatch.setattr(TrashReview, "_move", fail_second)
    with TestClient(create_app(lib_env.config)) as client:
        preview_id = client.post("/api/trash/preview-selected", json={
            "track_ids": [track.id for track in tracks],
        }).json()["job_id"]
        assert wait_job(client, preview_id)["status"] == "done"
        apply_id = client.post("/api/trash/apply-reviewed", json={
            "preview_job_id": preview_id,
        }).json()["job_id"]
        job = wait_job(client, apply_id)
        assert job["outcomes"] == {"completed": 1, "failed": 1}
        failures = client.get(f"/api/jobs/{apply_id}/items?status=failed").json()
        assert failures["total"] == 1
        assert failures["items"][0]["path"] == tracks[1].path
        assert "Read-only" in failures["items"][0]["error"]
        retry = client.get(f"/api/jobs/{apply_id}/retry-selection").json()
        assert [track["id"] for track in retry["tracks"]] == [tracks[1].id]
        assert lib_env.db.get_track(tracks[1].id).status != "deleted"
        assert client.post("/api/trash/apply-reviewed", json={
            "preview_job_id": preview_id,
        }).status_code == 400
        new_id = client.post("/api/trash/preview-selected", json={
            "track_ids": [track["id"] for track in retry["tracks"]],
        }).json()["job_id"]
        assert wait_job(client, new_id)["summary"]["ready"] == 1
        assert lib_env.db.get_track(tracks[1].id).status != "deleted"
    with TestClient(create_app(lib_env.config)) as client:
        assert client.get(f"/api/jobs/{apply_id}/items?status=failed").json()["total"] == 1


def test_issue_and_job_result_pages_do_not_truncate(lib_env, monkeypatch):
    for index in range(525):
        lib_env.db.upsert_track(Track(
            path=str(lib_env.library / f"{index:04}.flac"),
            status="bad", issues=[Issue("unparseable", f"Reason {index}", "bad")],
        ))
    with TestClient(create_app(lib_env.config)) as client:
        page = client.get("/api/issues/page?offset=500&limit=50").json()
        assert page["total"] == 525
        assert len(page["items"]) == 25
        assert page["items"][-1]["detail"] == "Reason 524"
        assert client.get("/api/issues/page?severity=warn").json()["total"] == 0
        assert client.get("/api/issues/page?offset=-1").status_code == 422
        job_id = client.post("/api/tasks/doctor", json={"deep": False}).json()["job_id"]
        assert wait_job(client, job_id)["summary"]["results"] == 525
        page = client.get(f"/api/jobs/{job_id}/results/results?offset=500").json()
        assert page["total"] == 525
        assert len(page["items"]) == 25


def test_fingerprints_are_generated_before_comparison(lib_env, monkeypatch):
    for index in range(2):
        lib_env.db.upsert_track(Track(
            path=f"/music/{index}.flac", title=f"Different {index}",
            album=f"Album {index}", duration=180 + index, status="suspect",
        ))
    calls = []

    def compute(config, db, tracks, progress):
        calls.append(len(tracks))
        for done, track in enumerate(tracks, 1):
            db.update_track_columns(track.id, fingerprint=",".join(
                str((index * 2654435761) & 0xFFFFFFFF) for index in range(32)
            ))
            progress(done, track.path)
        return len(tracks)

    monkeypatch.setattr("musiclib.server.app.fpcalc_available", lambda: "/usr/bin/fpcalc")
    monkeypatch.setattr("musiclib.server.app.compute_all", compute)
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/tasks/duplicates", json={"fingerprint": True}).json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert calls == [2]
        assert job["summary"]["groups"] == 1
        assert len(client.get(f"/api/jobs/{job_id}/results/groups").json()["items"][0]["members"]) == 2


def test_fingerprint_failures_are_explicit(lib_env, monkeypatch):
    monkeypatch.setattr("musiclib.server.app.fpcalc_available", lambda: None)
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/tasks/duplicates", json={"fingerprint": True}).json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "error"
        assert "fpcalc" in job["error"]
        assert client.get(f"/api/jobs/{job_id}/results/groups").status_code == 409


def test_deep_check_counts_each_file_once_and_shallow_preserves_decode_error(lib_env, monkeypatch):
    path = lib_env.library / "bad.flac"
    path.write_bytes(b"audio")
    lib_env.db.upsert_track(Track(
        path=str(path), duration=2, format="flac",
        issues=[Issue("missing_tags", "Missing title", "warn")],
    ))
    monkeypatch.setattr("musiclib.doctor.ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr("musiclib.doctor.find_binary", lambda name: name)
    monkeypatch.setattr("musiclib.doctor.decode_check_ffmpeg", lambda path: {"ok": False, "error": "broken"})
    report = Doctor(lib_env.config, lib_env.db).check(deep=True)
    assert report.bad == 1
    assert report.suspect == 0
    assert len(report.results) == 1
    shallow = Doctor(lib_env.config, lib_env.db).check(deep=False)
    assert shallow.bad == 1
    assert "decode_error" in shallow.results[0]["issues"]


def test_stop_request_keeps_lane_locked_until_safe_checkpoint(lib_env, monkeypatch):
    entered, finish = threading.Event(), threading.Event()

    def scan(self, **kwargs):
        kwargs["progress_state"](1, 3, "first file")
        entered.set()
        assert finish.wait(5)
        kwargs["progress_state"](1, 3, "before second file")
        return ScanReport(scanned=3)

    monkeypatch.setattr("musiclib.server.app.Scanner.scan", scan)
    with TestClient(create_app(lib_env.config)) as client:
        try:
            job_id = client.post("/api/scan", json={}).json()["job_id"]
            assert entered.wait(5)
            response = client.post(f"/api/jobs/{job_id}/cancel")
            assert response.status_code == 200
            assert response.json()["status"] == "stopping"
            assert client.post("/api/tasks/doctor", json={}).status_code == 409
            assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        finally:
            finish.set()
        job = wait_job(client, job_id)
        assert job["status"] == "cancelled"
        assert job["completed"] == 1
        assert job["summary"] == {"processed": 1, "total": 3}
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
        assert client.post("/api/jobs/not-found/cancel").status_code == 404
        assert client.post("/api/tasks/rename", json={}).status_code == 200
    with TestClient(create_app(lib_env.config)) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"


def test_queued_job_can_be_stopped_before_any_work(lib_env, monkeypatch):
    pending = []

    class Worker:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            pending.append(self.target)

        def join(self):
            pass

    registry = JobRegistry(lib_env.db)
    monkeypatch.setattr("musiclib.server.jobs.threading.Thread", Worker)
    job_id = registry.start("test", lambda job: pytest.fail("Queued work must not start"))
    assert registry.cancel(job_id)["status"] == "stopping"
    pending[0]()
    assert registry.get(job_id)["status"] == "cancelled"
    assert registry.operation(lambda: "available")() == "available"


@pytest.mark.parametrize("read_only", [False, True])
def test_cancellation_only_stops_the_requested_lane(lib_env, read_only):
    registry = JobRegistry(lib_env.db)
    entered = threading.Event()
    release = threading.Event()

    def run(job):
        entered.set()
        assert release.wait(5)
        registry.update("Next file", 1, 2)
        pytest.fail("No new work may start after a stop checkpoint")

    try:
        job_id = registry.start("stop-me", run, read_only=read_only)
        assert entered.wait(5)
        registry.cancel(job_id)
        other_id = registry.start("other-lane", lambda job: {}, read_only=not read_only)
    finally:
        release.set()
        registry.close()
    assert registry.get(job_id)["status"] == "cancelled"
    assert registry.get(other_id)["status"] == "done"


def test_selected_bad_file_trash_handles_more_than_one_page_or_batch(lib_env, monkeypatch):
    ids = []
    for index in range(225):
        path = lib_env.library / f"{index}.flac"
        path.write_bytes(b"bad")
        track = lib_env.db.upsert_track(Track(path=str(path), status="bad"))
        ids.append(track.id)
    monkeypatch.setattr("musiclib.doctor.trash_file", lambda path, root: path + ".trashed")
    with TestClient(create_app(lib_env.config)) as client:
        job_id = client.post("/api/issues/trash", json={"track_ids": ids}).json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert job["summary"]["trashed"] == 225
        assert job["completed"] == job["total"] == 225


def test_stopping_a_deep_check_preserves_last_known_decode_failure(lib_env, monkeypatch):
    from concurrent.futures import CancelledError
    path = lib_env.library / "bad.flac"
    path.write_bytes(b"bad audio")
    track = lib_env.db.upsert_track(Track(
        path=str(path), status="bad", format="flac",
        issues=[Issue("decode_error", "known corruption", "bad")],
    ))
    monkeypatch.setattr("musiclib.doctor.find_binary", lambda name: name)

    def stop_after_metadata(phase, completed, total, current):
        if completed:
            raise CancelledError()

    with pytest.raises(CancelledError):
        Doctor(lib_env.config, lib_env.db).check(deep=True, progress_state=stop_after_metadata)
    assert lib_env.db.get_track(track.id).status == "bad"
    assert any(i.kind == "decode_error" for i in lib_env.db.get_track(track.id).issues)
