import pytest
from conftest import JPEG_600x400, make_flac, make_mp3
from fastapi.testclient import TestClient
from test_jobs_api import wait_job

from musiclib.models import Track
from musiclib.scanner import Scanner
from musiclib.server.app import create_app


def test_album_page_groups_tracks_and_unknowns(lib_env):
    env = lib_env
    make_mp3(env.library / "a1.mp3", tags={
        "title": "One", "artist": "Known Artist", "album": "Night Songs",
        "albumartist": "Known Artist", "date": "2001",
    })
    make_mp3(env.library / "a2.mp3", tags={
        "title": "Two", "artist": "Known Artist", "album": "Night Songs",
        "albumartist": "Known Artist", "date": "2001",
    })
    make_mp3(env.library / "b1.mp3", tags={
        "title": "Other", "artist": "Other Artist", "album": "Other Album",
    })
    make_mp3(env.library / "loose.mp3")
    Scanner(env.config, env.db).scan()
    with TestClient(create_app(env.config)) as client:
        page = client.get("/api/albums/page").json()
        albums = {(item["album_artist"], item["album"]): item for item in page["items"]}
        assert page["total"] == 3
        assert albums[("Known Artist", "Night Songs")]["track_count"] == 2
        assert albums[("Known Artist", "Night Songs")]["year"] == 2001
        assert albums[("Other Artist", "Other Album")]["track_count"] == 1
        assert albums[("Unknown Artist", "Unknown Album")]["track_count"] == 1
        filtered = client.get("/api/albums/page", params={"query": "Night"}).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["album"] == "Night Songs"
        missing = client.get("/api/albums/detail", params={
            "album_artist": "Nobody", "album": "Missing",
        })
        assert missing.status_code == 404
        detail = client.get("/api/albums/detail", params={
            "album_artist": "Known Artist", "album": "Night Songs",
        }).json()
        assert detail["track_count"] == 2
        assert {track["title"] for track in detail["tracks"]} == {"One", "Two"}


def test_track_cover_serves_embedded_art(lib_env):
    env = lib_env
    make_flac(env.library / "cover.flac", tags={
        "title": "Art", "album": "Covered", "MUSICBRAINZ_ALBUMID": "catalog-release",
    }, art=JPEG_600x400)
    make_mp3(env.library / "plain.mp3", tags={"title": "Plain", "album": "Bare"})
    Scanner(env.config, env.db).scan()
    tracks = {track.title: track for track in env.db.all_tracks()}
    with TestClient(create_app(env.config)) as client:
        page = client.get("/api/albums/page").json()
        covered = next(item for item in page["items"] if item["album"] == "Covered")
        assert covered["has_art"] is True
        assert covered["cover_url"] == f"/api/tracks/{tracks['Art'].id}/cover?v={tracks['Art'].file_hash}"
        detail = client.get("/api/albums/detail", params={
            "album_artist": "Unknown Artist", "album": "Covered",
        }).json()
        assert detail["cover_url"] == covered["cover_url"]
        response = client.get(covered["cover_url"])
        assert response.status_code == 200
        assert response.content[:3] == b"\xff\xd8\xff"
        bare = next(item for item in page["items"] if item["album"] == "Bare")
        assert bare["cover_url"] is None
        assert client.get(f"/api/tracks/{tracks['Plain'].id}/cover").status_code == 404
        assert client.get("/api/tracks/999999/cover").status_code == 404


@pytest.mark.parametrize("limit", [50, 100, 150, 200])
def test_album_scroll_batches_cover_all_albums_without_overlap(lib_env, limit):
    for index in range(225):
        lib_env.db.upsert_track(Track(
            path=str(lib_env.library / f"{index}.mp3"), album=f"Album {index:03d}",
            album_artist="Artist",
        ))
    with TestClient(create_app(lib_env.config)) as client:
        first = client.get("/api/albums/page", params={"limit": limit}).json()
        second = client.get("/api/albums/page", params={"limit": limit, "offset": limit}).json()
        assert len(first["items"]) == limit
        assert len(second["items"]) == min(limit, 225 - limit)
        assert not {a["album"] for a in first["items"]} & {a["album"] for a in second["items"]}
        assert first["total"] == second["total"] == 225


def test_album_duplicates_are_scoped_recoverable_and_leave_organization_separate(mini_library):
    env = mini_library
    Scanner(env.config, env.db, workers=1).scan()
    from musiclib.duplicates import DuplicateFinder
    global_groups = DuplicateFinder(env.config, env.db).find()
    tracks = [m.track for m in global_groups[0].members]
    with TestClient(create_app(env.config)) as client:
        job_id = client.post("/api/dupes/selected-preview", json={
            "track_ids": [t.id for t in tracks],
        }).json()["job_id"]
        assert wait_job(client, job_id)["status"] == "done"
        groups = client.get(f"/api/jobs/{job_id}/results/groups").json()["items"]
        snapshots = client.get(f"/api/jobs/{job_id}/results/tracks").json()["items"]
        assert client.get("/api/dupes").json()[0]["group_id"] == global_groups[0].group_id
        keeper = groups[0]["keep_id"]
        body = {
            "groups": [{"track_ids": [m["id"] for m in groups[0]["members"]], "keep_id": keeper}],
            "snapshots": {t["id"]: {"path": t["path"], "file_hash": t["file_hash"]} for t in snapshots},
        }
        apply_id = client.post("/api/dupes/selected-apply", json=body).json()["job_id"]
        result = wait_job(client, apply_id)
        assert result["status"] == "done"
        assert result["summary"]["trashed"] == len(tracks) - 1
        remaining = client.get(f"/api/jobs/{apply_id}/results/remaining_ids").json()["items"]
        assert remaining == [keeper]
        assert env.db.get_track(keeper).path == next(t.path for t in tracks if t.id == keeper)
        assert len(client.get("/api/trash").json()["items"]) == len(tracks) - 1
        assert client.get("/api/stats").json()["dupe_groups"] == len(client.get("/api/dupes").json())
        assert client.post("/api/dupes/selected-apply", json=body).status_code == 409


def test_album_duplicate_apply_rejects_changed_keeper_and_overlapping_groups(mini_library):
    env = mini_library
    Scanner(env.config, env.db, workers=1).scan()
    tracks = env.db.all_tracks()[:2]
    snapshots = {t.id: {"path": t.path, "file_hash": t.file_hash} for t in tracks}
    group = {"track_ids": [t.id for t in tracks], "keep_id": tracks[0].id}
    with TestClient(create_app(env.config)) as client:
        assert client.post("/api/dupes/selected-apply", json={
            "groups": [group, group], "snapshots": snapshots,
        }).status_code == 400
        with open(tracks[0].path, "ab") as stream:
            stream.write(b"changed")
        job_id = client.post("/api/dupes/selected-apply", json={
            "groups": [group], "snapshots": snapshots,
        }).json()["job_id"]
        assert wait_job(client, job_id)["status"] == "error"
        assert env.db.query("SELECT * FROM deletions") == []


def test_bulk_duplicate_review_rejects_files_rescanned_since_selection(mini_library):
    from musiclib.duplicates import DuplicateFinder
    env = mini_library
    Scanner(env.config, env.db, workers=1).scan()
    DuplicateFinder(env.config, env.db).find()
    with TestClient(create_app(env.config)) as client:
        group = client.get("/api/dupes").json()[0]
        member = group["members"][0]
        env.db.update_track_columns(member["id"], file_hash="new scanned version")
        response = client.post("/api/dupes/apply", json={
            "group_ids": [group["group_id"]],
            "keep_ids": {group["group_id"]: group["keep_id"]},
            "member_ids": {group["group_id"]: [m["id"] for m in group["members"]]},
            "snapshots": {m["id"]: {"path": m["path"], "file_hash": m["file_hash"]} for m in group["members"]},
        })
        assert response.status_code == 409
        assert env.db.query("SELECT * FROM deletions") == []
