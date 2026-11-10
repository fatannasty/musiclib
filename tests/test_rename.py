import errno
import os
from pathlib import Path

import pytest
from conftest import ALBUM_A, make_flac, make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.rename import Renamer, sanitize_component
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


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


def test_rename_moves_root_tracks_to_singles_by_genre(lib_env):
    env = lib_env
    make_flac(
        env.library / "loose.flac",
        tags={"ARTIST": "Solo Artist", "TITLE": "Loose Song", "GENRE": "Jazz"},
    )
    Scanner(env.config, env.db).scan()
    report = Renamer(env.config, env.db).apply(Renamer(env.config, env.db).plan())
    target = env.library / "Singles" / "Jazz" / "Solo Artist - Loose Song.flac"
    assert report.moved == 1
    assert target.exists()
    assert Renamer(env.config, env.db).plan().moves == []


def test_root_singles_can_be_disabled(lib_env):
    env = lib_env
    env.config.organize["root_singles"] = False
    make_flac(
        env.library / "album-track.flac",
        tags={**ALBUM_A, "TITLE": "First Song", "TRACKNUMBER": "1"},
    )
    Scanner(env.config, env.db).scan()
    report = Renamer(env.config, env.db).plan()
    assert len(report.moves) == 1
    assert "Album A (2001)" in report.moves[0].target


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


def _selected_tracks(env, count=2):
    for index in range(count):
        make_flac(env.library / f"loose-{index}.flac", tags={
            **ALBUM_A, "TITLE": f"Song {index}", "TRACKNUMBER": str(index + 1),
        })
    Scanner(env.config, env.db).scan()
    return env.db.all_tracks()


def test_selected_preview_is_read_only_and_uses_album_templates(lib_env):
    env = lib_env
    tracks = _selected_tracks(env)
    renamer = Renamer(env.config, env.db)
    snapshot = [dict(row) for row in env.db.query("SELECT * FROM tracks ORDER BY id")]
    preview = renamer.preview_selected([tracks[0].id])
    assert preview["ready"] == 1
    assert preview["moves"][0]["target"] == renamer.target_path(tracks[0])
    assert "Singles" not in preview["moves"][0]["target"]
    assert [dict(row) for row in env.db.query("SELECT * FROM tracks ORDER BY id")] == snapshot
    assert all(Path(track.path).is_file() for track in tracks)
    assert not Path(preview["moves"][0]["target"]).parent.exists()
    assert renamer.plan([]).moves == []


def test_missing_selected_file_does_not_change_catalog_on_preview(lib_env):
    env = lib_env
    track = _selected_tracks(env, 1)[0]
    Path(track.path).unlink()
    status = env.db.get_track(track.id).status
    preview = Renamer(env.config, env.db).preview_selected([track.id])
    assert preview["ready"] == 0
    assert "missing" in preview["skipped_files"][0]["reason"]
    assert env.db.get_track(track.id).status == status


def test_selected_preview_survives_restart_and_moves_only_reviewed_files(lib_env):
    env = lib_env
    tracks = _selected_tracks(env)
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/rename/preview-selected", json={"track_ids": [tracks[0].id]})
        preview_id = response.json()["job_id"]
        preview = wait_job(client, preview_id)
        assert preview["summary"]["ready"] == 1
        assert preview["completed"] == preview["total"] == 1
        target = client.get(f"/api/jobs/{preview_id}/results/moves").json()["items"][0]["target"]
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/rename/apply-reviewed", json={"preview_job_id": preview_id})
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "done"
        assert job["summary"]["moved"] == 1
        assert job["summary"]["errors"] == 0
        assert Path(target).is_file()
        assert not Path(tracks[0].path).exists()
        assert env.db.get_track(tracks[0].id).path == target
        assert env.db.get_track(tracks[1].id).path == tracks[1].path
        assert Path(tracks[1].path).exists()
        retry = client.post("/api/rename/apply-reviewed", json={"preview_job_id": preview_id})
        retried = wait_job(client, retry.json()["job_id"])
        assert retried["summary"]["moved"] == 0
        assert retried["summary"]["errors"] == 1


@pytest.mark.parametrize("collision", ["existing", "case", "selected"])
def test_selected_preview_blocks_collisions_without_suffixes(lib_env, collision):
    env = lib_env
    tracks = _selected_tracks(env)
    renamer = Renamer(env.config, env.db)
    target = Path(renamer.target_path(tracks[0]))
    if collision == "selected":
        env.db.update_track_columns(tracks[1].id, title=tracks[0].title, track_no=tracks[0].track_no)
        ids = [track.id for track in tracks]
    else:
        target.parent.mkdir(parents=True)
        occupied = target if collision == "existing" else target.with_name(target.name.upper())
        occupied.write_bytes(b"Do not overwrite")
        ids = [tracks[0].id]
    preview = renamer.preview_selected(ids)
    assert preview["ready"] == 0
    assert preview["blocked"] == len(ids)
    assert all(move["target"] == str(target) for move in preview["moves"])
    assert all(move["reason"] for move in preview["moves"])
    assert all(Path(track.path).exists() for track in tracks)


@pytest.mark.parametrize("denied", ["source", "target"])
def test_selected_preview_reports_nas_permissions(lib_env, monkeypatch, denied):
    env = lib_env
    _selected_tracks(env)
    source = env.library / "incoming"
    source.mkdir()
    make_flac(source / "one.flac", tags={**ALBUM_A, "TITLE": "Nested", "TRACKNUMBER": "3"})
    Scanner(env.config, env.db).scan()
    track = next(track for track in env.db.all_tracks() if track.title == "Nested")
    blocked_dir = source if denied == "source" else env.library
    monkeypatch.setattr("musiclib.rename.os.access", lambda path, mode: Path(path) != blocked_dir)
    preview = Renamer(env.config, env.db).preview_selected([track.id])
    assert preview["ready"] == 0
    assert preview["blocked"] == 1
    assert "PUID/PGID" in preview["moves"][0]["reason"]


def test_selected_apply_rechecks_changes_and_collisions_and_continues(lib_env):
    env = lib_env
    tracks = _selected_tracks(env, 3)
    renamer = Renamer(env.config, env.db)
    preview = renamer.preview_selected([track.id for track in tracks])
    assert preview["ready"] == 3
    with open(tracks[0].path, "ab") as file:
        file.write(b"changed")
    occupied = Path(preview["moves"][1]["target"])
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"new occupant")
    result = renamer.apply_selected(preview)
    assert result["moved"] == 1
    assert len(result["errors"]) == 2
    assert "changed since preview" in result["errors"][0]["error"]
    assert "occupied" in result["errors"][1]["error"]
    assert occupied.read_bytes() == b"new occupant"
    assert Path(tracks[0].path).exists()
    assert Path(tracks[1].path).exists()
    assert not Path(tracks[2].path).exists()


def test_selected_apply_catches_permission_errors_per_file(lib_env, monkeypatch):
    env = lib_env
    tracks = _selected_tracks(env)
    renamer = Renamer(env.config, env.db)
    preview = renamer.preview_selected([track.id for track in tracks])
    move = renamer._move_no_replace

    def permission_error(source, target):
        if source == tracks[0].path:
            raise PermissionError("NAS share is read-only")
        move(source, target)

    monkeypatch.setattr(renamer, "_move_no_replace", permission_error)
    result = renamer.apply_selected(preview)
    assert result["moved"] == 1
    assert len(result["errors"]) == 1
    assert "read-only" in result["errors"][0]["error"]
    assert env.db.get_track(tracks[0].id).path == tracks[0].path


@pytest.mark.parametrize("cross_device", [False, True])
def test_selected_move_cannot_overwrite_a_racing_target(lib_env, monkeypatch, cross_device):
    env = lib_env
    track = _selected_tracks(env, 1)[0]
    renamer = Renamer(env.config, env.db)
    preview = renamer.preview_selected([track.id])
    target = Path(preview["moves"][0]["target"])
    original = Path(track.path).read_bytes()

    def race(source, destination, **kwargs):
        Path(destination).write_bytes(b"racing occupant")
        if cross_device:
            raise OSError(errno.EXDEV, "Cross-device link")
        raise FileExistsError(errno.EEXIST, "Target now exists")

    monkeypatch.setattr("musiclib.rename.os.link", race)
    result = renamer.apply_selected(preview)
    assert result["moved"] == 0
    assert len(result["errors"]) == 1
    assert target.read_bytes() == b"racing occupant"
    assert Path(track.path).read_bytes() == original


@pytest.mark.parametrize("link_error", [errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP])
def test_selected_move_without_links_and_unlink_failure(lib_env, monkeypatch, link_error):
    env = lib_env
    tracks = _selected_tracks(env)
    renamer = Renamer(env.config, env.db)
    preview = renamer.preview_selected([track.id for track in tracks])
    originals = [Path(track.path).read_bytes() for track in tracks]
    unlink = os.unlink

    def cross_device(*args, **kwargs):
        raise OSError(link_error, "Hard links are unavailable")

    def blocked_unlink(path, *args, **kwargs):
        if str(path) == tracks[0].path:
            raise PermissionError("Cannot remove source")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr("musiclib.rename.os.link", cross_device)
    monkeypatch.setattr("musiclib.rename.os.unlink", blocked_unlink)
    result = renamer.apply_selected(preview)
    assert result["moved"] == 1
    assert len(result["errors"]) == 1
    assert Path(tracks[0].path).read_bytes() == originals[0]
    assert not Path(preview["moves"][0]["target"]).exists()
    assert Path(preview["moves"][1]["target"]).read_bytes() == originals[1]


def test_selected_preview_rejects_outside_symlinks_and_changed_templates(lib_env, tmp_path):
    env = lib_env
    tracks = _selected_tracks(env)
    outside = tmp_path / "outside.flac"
    Path(tracks[0].path).rename(outside)
    Path(tracks[0].path).symlink_to(outside)
    renamer = Renamer(env.config, env.db)
    preview = renamer.preview_selected([track.id for track in tracks])
    assert len(preview["skipped_files"]) == 1
    assert preview["ready"] == 1
    env.config.organize["template"] = "{title}.{ext}"
    with pytest.raises(ValueError, match="templates changed"):
        Renamer(env.config, env.db).apply_selected(preview)
    assert Path(tracks[1].path).exists()
    assert outside.exists()


@pytest.mark.parametrize("track_ids", [[], [1, 1], list(range(201))])
def test_selected_rename_api_rejects_invalid_batches(lib_env, track_ids):
    with TestClient(create_app(lib_env.config)) as client:
        assert client.post("/api/rename/preview-selected", json={"track_ids": track_ids}).status_code == 400
        assert client.post("/api/rename/apply-reviewed", json={"preview_job_id": "missing"}).status_code == 400
        assert client.get("/api/jobs").json() == []
