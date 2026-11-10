import errno
import sqlite3
from pathlib import Path

import pytest
from conftest import ALBUM_A, JPEG_600x400, make_flac
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

import musiclib.fsops as fsops
import musiclib.restore_review as review_module
from musiclib.fsops import trash_file
from musiclib.restore_review import RestoreReview
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


def trashed_tracks(env, count=3):
    for index in range(count):
        make_flac(env.library / "Artist" / "Album" / f"{index}.flac", tags={
            **ALBUM_A, "TITLE": f"Song {index}", "TRACKNUMBER": str(index + 1),
        }, art=JPEG_600x400)
    Scanner(env.config, env.db, workers=1).scan()
    entries = []
    for track in env.db.all_tracks():
        target = trash_file(track.path, env.config.trash_dir)
        deletion_id = env.db.record_deletion(
            track.path, track.size, track.file_hash, "test", target,
        )
        env.db.update_track_columns(track.id, status="deleted")
        entries.append({
            "id": deletion_id, "track_id": track.id, "path": track.path,
            "moved_to": target, "data": Path(target).read_bytes(),
        })
    return entries


@pytest.mark.parametrize("copy_fallback", [False, True])
def test_reviewed_restore_survives_restart_and_uses_only_saved_targets(lib_env, monkeypatch, copy_fallback):
    env = lib_env
    entries = trashed_tracks(env)
    parent = Path(entries[0]["path"]).parent
    parent.rmdir()
    if copy_fallback:
        def cross_device(*args, **kwargs):
            raise OSError(errno.EXDEV, "Cross-device link")
        monkeypatch.setattr(fsops.os, "link", cross_device)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/trash/restore-preview", json={
            "deletion_ids": [entry["id"] for entry in entries[:2]],
        })
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        preview = wait_job(client, job_id)
        assert (preview["summary"]["ready"], preview["summary"]["blocked"]) == (2, 0)
        review_id = preview["summary"]["review_id"]
        targets = client.get(f"/api/jobs/{job_id}/results/entries").json()["items"]
        assert {item["target"] for item in targets} == {entry["path"] for entry in entries[:2]}
        assert not parent.exists()
        assert all(Path(entry["moved_to"]).read_bytes() == entry["data"] for entry in entries)
    with TestClient(create_app(env.config)) as client:
        assert client.get(f"/api/trash/restore-reviews/{review_id}").json() == {"status": "review"}
        response = client.post("/api/trash/restore-reviewed", json={
            "preview_job_id": job_id, "deletion_ids": [entries[2]["id"]],
            "target": str(env.library / "unreviewed.flac"),
        })
        assert response.status_code == 200
        assert wait_job(client, response.json()["job_id"])["summary"] == {"restored": 2, "errors": 0}
        for entry in entries[:2]:
            assert Path(entry["path"]).read_bytes() == entry["data"]
            assert not Path(entry["moved_to"]).exists()
            assert env.db.get_track(entry["track_id"]).status == "suspect"
        assert Path(entries[2]["moved_to"]).read_bytes() == entries[2]["data"]
        assert not Path(entries[2]["path"]).exists()
        assert not (env.library / "unreviewed.flac").exists()
        assert client.get("/api/trash").json()["total"] == 1
        assert client.post("/api/trash/restore-reviewed", json={
            "preview_job_id": job_id,
        }).status_code == 400
        assert client.get(f"/api/trash/restore-reviews/{review_id}").json() == {"status": "used"}


def test_restore_preview_blocks_occupied_unsafe_and_changed_entries(lib_env, tmp_path):
    env = lib_env
    entries = trashed_tracks(env, 5)
    Path(entries[0]["path"]).write_bytes(b"existing original")
    Path(entries[1]["moved_to"]).unlink()
    Path(entries[2]["moved_to"]).write_bytes(b"changed before preview")
    linked_source = Path(entries[3]["moved_to"])
    linked_source.unlink()
    linked_source.symlink_to(entries[4]["moved_to"])
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")
    external_id = env.db.record_deletion(entries[4]["path"], 7, None, "test", str(outside))
    preview = RestoreReview(env.config, env.db).preview([
        *[entry["id"] for entry in entries[:4]], external_id, 999,
    ])
    assert (preview["ready"], preview["blocked"]) == (0, 6)
    reasons = [entry["reason"] for entry in preview["entries"]]
    assert "occupied" in reasons[0]
    assert "missing" in reasons[1]
    assert "differs" in reasons[2]
    assert "Symbolic" in reasons[3]
    assert "outside" in reasons[4]
    assert "unavailable" in reasons[5]
    assert Path(entries[0]["path"]).read_bytes() == b"existing original"
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("duplicate", ["source", "target"])
def test_restore_preview_blocks_both_duplicate_sources_or_targets(lib_env, duplicate):
    env = lib_env
    entries = trashed_tracks(env, 2)
    column = "moved_to" if duplicate == "source" else "path"
    env.db.execute(f"UPDATE deletions SET {column}=? WHERE id=?", (entries[0][column], entries[1]["id"]))
    if duplicate == "source":
        env.db.execute("UPDATE deletions SET file_hash=NULL WHERE id=?", (entries[1]["id"],))
    preview = RestoreReview(env.config, env.db).preview([entry["id"] for entry in entries])
    assert (preview["ready"], preview["blocked"]) == (0, 2)
    assert all("share" in entry["reason"] for entry in preview["entries"])
    assert all(Path(entry["moved_to"]).exists() for entry in entries)


@pytest.mark.parametrize("change", ["bytes", "entry", "restored", "symlink", "occupied"])
def test_restore_skips_changes_since_preview_but_continues(lib_env, change):
    env = lib_env
    entries = trashed_tracks(env, 2)
    reviewer = RestoreReview(env.config, env.db)
    preview = reviewer.preview([entry["id"] for entry in entries])
    first = entries[0]
    source = Path(first["moved_to"])
    if change == "bytes":
        source.write_bytes(b"changed since preview")
    elif change == "entry":
        env.db.execute("UPDATE deletions SET path=? WHERE id=?", (
            str(env.library / "unreviewed.flac"), first["id"],
        ))
    elif change == "restored":
        env.db.mark_restored(first["id"])
    elif change == "symlink":
        source.unlink()
        source.symlink_to(entries[1]["moved_to"])
    else:
        Path(first["path"]).write_bytes(b"new occupant")
    result = reviewer.apply(preview["review_id"])
    assert len(result["restored"]) == 1
    assert result["restored"][0]["deletion_id"] == entries[1]["id"]
    assert len(result["errors"]) == 1
    assert source.is_symlink() if change == "symlink" else source.exists()
    if change == "occupied":
        assert Path(first["path"]).read_bytes() == b"new occupant"
    else:
        assert not Path(first["path"]).exists()
    assert not (env.library / "unreviewed.flac").exists()
    assert Path(entries[1]["path"]).read_bytes() == entries[1]["data"]


def test_case_only_conflict_and_active_catalog_path_are_blocked(lib_env):
    env = lib_env
    entries = trashed_tracks(env, 2)
    first = Path(entries[0]["path"])
    first.with_name(first.name.upper()).write_bytes(b"case-only occupant")
    env.db.update_track_columns(entries[1]["track_id"], status="ok")
    preview = RestoreReview(env.config, env.db).preview([entry["id"] for entry in entries])
    assert (preview["ready"], preview["blocked"]) == (0, 2)
    assert "case-only" in preview["entries"][0]["reason"]
    assert "active catalog" in preview["entries"][1]["reason"]


@pytest.mark.parametrize("failure", ["permissions", "changed-copy", "changed-source", "history", "catalog"])
def test_restore_failures_keep_sources_or_report_completed_moves(lib_env, monkeypatch, failure):
    env = lib_env
    entries = trashed_tracks(env, 2)
    first = entries[0]
    reviewer = RestoreReview(env.config, env.db)
    preview = reviewer.preview([entry["id"] for entry in entries])
    if failure == "permissions":
        move = review_module.move_no_replace

        def deny_one(source, target, **kwargs):
            if source == first["moved_to"]:
                raise PermissionError("Read-only original folder")
            return move(source, target, **kwargs)

        monkeypatch.setattr(review_module, "move_no_replace", deny_one)
    elif failure in ("changed-copy", "changed-source"):
        copy = fsops.shutil.copyfileobj

        def cross_device(*args, **kwargs):
            raise OSError(errno.EXDEV, "Copy required")

        def damaged_copy(source, target):
            if str(target.name) == first["path"] and failure == "changed-copy":
                target.write(b"bad copy")
            else:
                copy(source, target)
                if str(target.name) == first["path"]:
                    Path(first["moved_to"]).write_bytes(b"external edit during restore")

        monkeypatch.setattr(fsops.os, "link", cross_device)
        monkeypatch.setattr(fsops.shutil, "copyfileobj", damaged_copy)
    else:
        if failure == "history":
            mark = env.db.mark_restored

            def mark_error(deletion_id):
                if deletion_id == first["id"]:
                    raise sqlite3.OperationalError("History database unavailable")
                return mark(deletion_id)

            monkeypatch.setattr(env.db, "mark_restored", mark_error)
        else:
            update = env.db.update_track_columns

            def catalog_error(track_id, **columns):
                if track_id == first["track_id"]:
                    raise sqlite3.OperationalError("Catalog database unavailable")
                return update(track_id, **columns)

            monkeypatch.setattr(env.db, "update_track_columns", catalog_error)
    result = reviewer.apply(preview["review_id"])
    assert len(result["errors"]) == 1
    assert len(result["restored"]) == (2 if failure in ("history", "catalog") else 1)
    assert Path(entries[1]["path"]).read_bytes() == entries[1]["data"]
    if failure in ("history", "catalog"):
        assert "File restored to" in result["errors"][0]["error"]
        assert first["path"] in result["errors"][0]["error"]
        assert Path(first["path"]).read_bytes() == first["data"]
        assert not Path(first["moved_to"]).exists()
    else:
        assert not Path(first["path"]).exists()
        expected = b"external edit during restore" if failure == "changed-source" else first["data"]
        assert Path(first["moved_to"]).read_bytes() == expected
    with pytest.raises(ValueError, match="already used"):
        reviewer.apply(preview["review_id"])


def test_restore_settings_change_requires_new_preview(lib_env):
    env = lib_env
    entry = trashed_tracks(env, 1)[0]
    preview = RestoreReview(env.config, env.db).preview([entry["id"]])
    old_trash = env.config.trash_dir
    env.config.trash_dir = str(env.library / ".new-trash")
    with pytest.raises(ValueError, match="settings changed"):
        RestoreReview(env.config, env.db).apply(preview["review_id"])
    assert Path(entry["moved_to"]).read_bytes() == entry["data"]
    env.config.trash_dir = old_trash
    with pytest.raises(ValueError, match="already used"):
        RestoreReview(env.config, env.db).apply(preview["review_id"])


@pytest.mark.parametrize("unsafe", ["outside", "trash", "symlink"])
def test_restore_preview_refuses_unsafe_original_paths(lib_env, tmp_path, unsafe):
    env = lib_env
    entry = trashed_tracks(env, 1)[0]
    if unsafe == "outside":
        target = tmp_path / "outside" / "song.flac"
    elif unsafe == "trash":
        target = Path(env.config.trash_dir) / "nested-original.flac"
    else:
        target = Path(entry["path"])
        target.symlink_to(entry["moved_to"])
    env.db.execute("UPDATE deletions SET path=? WHERE id=?", (str(target), entry["id"]))
    preview = RestoreReview(env.config, env.db).preview([entry["id"]])
    assert (preview["ready"], preview["blocked"]) == (0, 1)
    reason = {"outside": "outside", "trash": "inside trash", "symlink": "Symbolic"}[unsafe]
    assert reason in preview["entries"][0]["reason"]
    assert Path(entry["moved_to"]).read_bytes() == entry["data"]


def test_restore_api_rejects_bad_selection_or_wrong_job(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        for ids in ([], [1, 1], list(range(201))):
            assert client.post("/api/trash/restore-preview", json={
                "deletion_ids": ids,
            }).status_code == 400
        assert client.post("/api/trash/restore-reviewed", json={
            "preview_job_id": "not-a-preview",
        }).status_code == 400
        assert client.get("/api/jobs").json() == []


def test_restore_accepts_old_entries_without_hashes_and_blocks_empty_ready_list(lib_env):
    env = lib_env
    entry = trashed_tracks(env, 1)[0]
    env.db.execute("UPDATE deletions SET file_hash=NULL WHERE id=?", (entry["id"],))
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/trash/restore-preview", json={"deletion_ids": [entry["id"]]})
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"]["ready"] == 1
        response = client.post("/api/trash/restore-reviewed", json={"preview_job_id": job_id})
        assert wait_job(client, response.json()["job_id"])["summary"] == {"restored": 1, "errors": 0}
        assert Path(entry["path"]).read_bytes() == entry["data"]
        response = client.post("/api/trash/restore-preview", json={
            "deletion_ids": list(range(1000, 1200)),
        })
        job_id = response.json()["job_id"]
        assert wait_job(client, job_id)["summary"]["blocked"] == 200
        assert client.post("/api/trash/restore-reviewed", json={"preview_job_id": job_id}).status_code == 400
