import errno
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import ALBUM_A, JPEG_600x400, make_flac
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

import musiclib.fsops as fsops
import musiclib.trash_review as review_module
from musiclib.models import Track
from musiclib.scanner import Scanner, file_hash
from musiclib.server.app import create_app
from musiclib.trash_review import TrashReview


def selected_tracks(env, count=3):
    for index in range(count):
        make_flac(env.library / f"song-{index}.flac", tags={
            **ALBUM_A, "TITLE": f"Song {index}", "TRACKNUMBER": str(index + 1),
        }, art=JPEG_600x400)
    Scanner(env.config, env.db, workers=1).scan()
    return env.db.all_tracks()


@pytest.mark.parametrize("copy_fallback", [False, True])
def test_saved_trash_preview_apply_and_restore(lib_env, monkeypatch, copy_fallback):
    env = lib_env
    tracks = selected_tracks(env)
    before = {track.id: Path(track.path).read_bytes() for track in tracks}
    folder_cover = env.library / "folder.jpg"
    folder_cover.write_bytes(b"folder artwork")
    if copy_fallback:
        def cross_device(*args, **kwargs):
            raise OSError(errno.EXDEV, "Cross-device link")
        monkeypatch.setattr(fsops.os, "link", cross_device)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/trash/preview-selected", json={
            "track_ids": [tracks[0].id, tracks[1].id],
        })
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        preview = wait_job(client, job_id)
        assert preview["summary"]["ready"] == 2
        assert preview["summary"]["blocked"] == 0
        review_id = preview["summary"]["review_id"]
        targets = client.get(f"/api/jobs/{job_id}/results/tracks").json()["items"]
        assert {item["track_id"] for item in targets} == {tracks[0].id, tracks[1].id}
        assert not Path(env.config.trash_dir).exists()
        assert client.get("/api/trash").json()["total"] == 0
        for track in tracks:
            assert Path(track.path).read_bytes() == before[track.id]
    with TestClient(create_app(env.config)) as client:
        assert client.get(f"/api/trash/reviews/{review_id}").json() == {"status": "review"}
        response = client.post("/api/trash/apply-reviewed", json={
            "preview_job_id": job_id, "track_ids": [tracks[2].id], "hard_delete": True,
        })
        assert response.status_code == 200
        result = wait_job(client, response.json()["job_id"])
        assert result["summary"] == {"trashed": 2, "errors": 0}
        entries = client.get("/api/trash").json()["items"]
        assert len(entries) == 2
        assert {item["moved_to"] for item in entries} == {item["target"] for item in targets}
        assert Path(tracks[2].path).read_bytes() == before[tracks[2].id]
        assert folder_cover.read_bytes() == b"folder artwork"
        for track in tracks[:2]:
            assert not Path(track.path).exists()
            assert env.db.get_track(track.id).status == "deleted"
        for entry in entries:
            track = next(track for track in tracks if track.path == entry["path"])
            assert Path(entry["moved_to"]).read_bytes() == before[track.id]
            assert entry["file_hash"] == file_hash(entry["moved_to"])
            restored = client.post("/api/trash/restore", json={"deletion_id": entry["id"]})
            assert restored.status_code == 200
            assert Path(track.path).read_bytes() == before[track.id]
        assert client.get("/api/trash").json()["total"] == 0
        assert client.post("/api/trash/apply-reviewed", json={
            "preview_job_id": job_id,
        }).status_code == 400
        for track in tracks:
            assert Path(track.path).read_bytes() == before[track.id]


def test_preview_blocks_unsafe_sources_but_allows_unhashed_bad_file(lib_env, tmp_path):
    env = lib_env
    corrupt = env.library / "corrupt.flac"
    corrupt.write_bytes(b"unparseable music")
    bad = env.db.upsert_track(Track(path=str(corrupt), status="bad"))
    missing = env.db.upsert_track(Track(path=str(env.library / "missing.flac")))
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")
    external = env.db.upsert_track(Track(path=str(outside)))
    link = env.library / "linked.flac"
    link.symlink_to(corrupt)
    linked = env.db.upsert_track(Track(path=str(link)))
    reviewer = TrashReview(env.config, env.db)
    preview = reviewer.preview([bad.id, missing.id, external.id, linked.id, 999])
    assert (preview["ready"], preview["blocked"]) == (1, 4)
    assert "missing" in preview["tracks"][1]["reason"]
    assert "outside" in preview["tracks"][2]["reason"]
    assert "Symbolic" in preview["tracks"][3]["reason"]
    assert preview["tracks"][0]["file_hash"] == file_hash(str(corrupt))
    result = reviewer.apply(preview["review_id"])
    assert len(result["trashed"]) == 1
    assert result["errors"] == []
    assert outside.read_bytes() == b"outside"
    assert link.is_symlink()
    assert Path(result["trashed"][0]["target"]).read_bytes() == b"unparseable music"


@pytest.mark.parametrize("change", ["bytes", "path", "missing", "symlink"])
def test_apply_refuses_sources_changed_since_preview(lib_env, change):
    env = lib_env
    tracks = selected_tracks(env, count=1)
    track = tracks[0]
    source = Path(track.path)
    reviewer = TrashReview(env.config, env.db)
    preview = reviewer.preview([track.id])
    if change == "bytes":
        source.write_bytes(source.read_bytes() + b"external edit")
    elif change == "missing":
        source.unlink()
    else:
        moved = source.rename(source.with_name("moved.flac"))
        if change == "path":
            env.db.update_track_columns(track.id, path=str(moved))
        else:
            source.symlink_to(moved)
    before = {str(path): path.read_bytes() for path in env.library.glob("*.flac")}
    result = reviewer.apply(preview["review_id"])
    assert result["trashed"] == []
    assert len(result["errors"]) == 1
    assert env.db.deletions_page()[1] == 0
    for path, data in before.items():
        assert Path(path).read_bytes() == data
    assert reviewer.status(preview["review_id"]) == "used"
    with pytest.raises(ValueError, match="already used"):
        reviewer.apply(preview["review_id"])


def test_conflicts_and_permission_failures_do_not_stop_remaining_files(lib_env, monkeypatch):
    env = lib_env
    tracks = selected_tracks(env)
    reviewer = TrashReview(env.config, env.db)
    preview = reviewer.preview([track.id for track in tracks])
    occupied = Path(preview["tracks"][0]["target"])
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"do not overwrite")
    move = review_module.move_no_replace

    def deny_second(source, target, **kwargs):
        if source == tracks[1].path:
            raise PermissionError("Read-only source folder")
        return move(source, target, **kwargs)

    monkeypatch.setattr(review_module, "move_no_replace", deny_second)
    result = reviewer.apply(preview["review_id"])
    assert len(result["trashed"]) == 1
    assert len(result["errors"]) == 2
    assert "occupied" in result["errors"][0]["error"]
    assert "Read-only" in result["errors"][1]["error"]
    assert occupied.read_bytes() == b"do not overwrite"
    assert all(Path(track.path).exists() for track in tracks[:2])
    assert not Path(tracks[2].path).exists()
    assert env.db.deletions_page()[1] == 1


@pytest.mark.parametrize("change", ["source", "copy", "replaced-target"])
def test_changed_source_or_invalid_copy_is_not_removed(lib_env, monkeypatch, change):
    env = lib_env
    track = selected_tracks(env, count=1)[0]
    source = Path(track.path)
    reviewer = TrashReview(env.config, env.db)
    preview = reviewer.preview([track.id])
    before = source.read_bytes()
    external = before + b"external edit during copy"
    copy = fsops.shutil.copyfileobj

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Copy required")

    def edit_after_copy(src, dest):
        if change == "copy":
            dest.write(b"damaged copy")
        else:
            copy(src, dest)
            if change == "source":
                source.write_bytes(external)
            else:
                Path(dest.name).unlink()
                Path(dest.name).write_bytes(b"another process owns this file")

    monkeypatch.setattr(fsops.os, "link", cross_device)
    monkeypatch.setattr(fsops.shutil, "copyfileobj", edit_after_copy)
    result = reviewer.apply(preview["review_id"])
    assert result["trashed"] == []
    expected_error = {
        "source": "changed since preview", "copy": "copy differs",
        "replaced-target": "Destination changed",
    }[change]
    assert expected_error in result["errors"][0]["error"]
    assert source.read_bytes() == (external if change == "source" else before)
    target = Path(preview["tracks"][0]["target"])
    if change == "replaced-target":
        assert target.read_bytes() == b"another process owns this file"
    else:
        assert not target.exists()
    assert env.db.deletions_page()[1] == 0


@pytest.mark.parametrize("failure", ["history", "unlink", "catalog", "changed-during-history"])
def test_recovery_history_survives_only_completed_moves(lib_env, monkeypatch, failure):
    env = lib_env
    track = selected_tracks(env, count=1)[0]
    source = Path(track.path)
    before = source.read_bytes()
    reviewer = TrashReview(env.config, env.db)
    preview = reviewer.preview([track.id])

    def database_error(*args, **kwargs):
        raise sqlite3.OperationalError("Database unavailable")

    if failure == "history":
        monkeypatch.setattr(env.db, "record_deletion", database_error)
    elif failure == "changed-during-history":
        record = env.db.record_deletion

        def edit_during_history(*args, **kwargs):
            deletion_id = record(*args, **kwargs)
            source.write_bytes(before + b"external edit during database write")
            return deletion_id

        monkeypatch.setattr(env.db, "record_deletion", edit_during_history)
    elif failure == "catalog":
        monkeypatch.setattr(env.db, "update_track_columns", database_error)
    else:
        unlink = os.unlink

        def deny_source(path, *args, **kwargs):
            if str(path) == str(source):
                raise PermissionError("Cannot remove original")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(fsops.os, "unlink", deny_source)
    result = reviewer.apply(preview["review_id"])
    assert len(result["errors"]) == 1
    if failure == "catalog":
        assert len(result["trashed"]) == 1
        assert "recoverable" in result["errors"][0]["error"]
        assert not source.exists()
        entries, total = env.db.deletions_page()
        assert total == 1
        assert Path(entries[0]["moved_to"]).read_bytes() == before
    else:
        assert result["trashed"] == []
        expected = (
            before + b"external edit during database write"
            if failure == "changed-during-history" else before
        )
        assert source.read_bytes() == expected
        assert not Path(preview["tracks"][0]["target"]).exists()
        assert env.db.deletions_page()[1] == 0


def test_changed_settings_require_fresh_trash_preview(lib_env):
    env = lib_env
    track = selected_tracks(env, count=1)[0]
    preview = TrashReview(env.config, env.db).preview([track.id])
    previous_trash = env.config.trash_dir
    env.config.trash_dir = str(env.library / ".different-trash")
    with pytest.raises(ValueError, match="settings changed"):
        TrashReview(env.config, env.db).apply(preview["review_id"])
    assert Path(track.path).exists()
    assert env.db.deletions_page()[1] == 0
    env.config.trash_dir = previous_trash
    with pytest.raises(ValueError, match="already used"):
        TrashReview(env.config, env.db).apply(preview["review_id"])


def test_trash_api_rejects_invalid_selection_and_non_preview_jobs(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        for ids in ([], [1, 1], list(range(201))):
            assert client.post("/api/trash/preview-selected", json={
                "track_ids": ids,
            }).status_code == 400
        assert client.post("/api/trash/apply-reviewed", json={
            "preview_job_id": "not-a-preview",
        }).status_code == 400
        assert client.get("/api/jobs").json() == []
