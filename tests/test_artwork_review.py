import base64
import io
import shutil
import subprocess
from pathlib import Path

import mutagen
import pytest
from conftest import make_flac, make_mp3
from fastapi.testclient import TestClient
from mutagen.flac import Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1
from mutagen.mp4 import MP4Cover
from PIL import Image
from test_jobs_api import wait_job

import musiclib.artwork_review as review_module
from musiclib.artwork import read_cover
from musiclib.artwork_review import ArtworkReview, validate_cover
from musiclib.models import Issue, Track
from musiclib.scanner import Scanner, file_hash
from musiclib.server.app import create_app


def image_bytes(fmt="PNG", size=(400, 400), color="blue"):
    output = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, format=fmt)
    return output.getvalue()


def encoded_image(data=None):
    return base64.b64encode(image_bytes() if data is None else data).decode("ascii")


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_review_changes_only_selected_cover_and_preserves_other_pictures_and_tags(lib_env, fmt):
    env = lib_env
    path = env.library / f"selected.{fmt}"
    sibling = env.library / "sibling.mp3"
    if fmt == "flac":
        make_flac(path, tags={
            "TITLE": "Song", "ARTIST": "Artist", "ALBUM": "Album", "TRACKNUMBER": "2",
            "DATE": "2004", "MUSICBRAINZ_ALBUMID": "existing-release",
        })
    else:
        make_mp3(path, tags={
            "title": "Song", "artist": "Artist", "album": "Album", "tracknumber": "2", "date": "2004",
        })
    make_mp3(sibling, tags={"title": "Sibling", "artist": "Artist", "album": "Album"})
    old, back, new = image_bytes(color="red"), image_bytes(color="green"), image_bytes()
    audio = mutagen.File(path)
    if fmt == "flac":
        for kind, data in ((3, old), (4, back)):
            picture = Picture()
            picture.type, picture.mime, picture.data = kind, "image/png", data
            audio.add_picture(picture)
    else:
        audio.tags.add(APIC(type=3, mime="image/png", desc="Old", data=old))
        audio.tags.add(APIC(type=4, mime="image/png", desc="Front cover", data=back))
    audio.save()
    folder = env.library / "folder.jpg"
    folder.write_bytes(image_bytes(fmt="JPEG"))
    Scanner(env.config, env.db).scan()
    track = env.db.find_track_by_path(str(path))
    untouched = sibling.read_bytes(), folder.read_bytes(), path.read_bytes()
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview([track.id], encoded_image(new))
    assert preview["ready"] == 1
    assert preview["warnings"] == []
    assert path.read_bytes() == untouched[2]
    assert reviewer.cover(preview["review_id"]) == (new, "image/png")
    result = reviewer.apply(preview["review_id"])
    assert len(result["applied"]) == 1 and result["errors"] == []
    assert sibling.read_bytes() == untouched[0]
    assert folder.read_bytes() == untouched[1]
    assert read_cover(str(path)) == (new, "image/png")
    updated = env.db.get_track(track.id)
    assert updated.file_hash == file_hash(str(path)) != track.file_hash
    assert updated.has_embedded_art
    for field in ("title", "artist", "album", "album_artist", "year", "track_no", "mbid_release"):
        assert getattr(updated, field) == getattr(track, field)
    audio = mutagen.File(path)
    pictures = audio.pictures if fmt == "flac" else audio.tags.getall("APIC")
    assert sorted((int(picture.type), picture.data) for picture in pictures) == [(3, new), (4, back)]
    with pytest.raises(ValueError, match="already used"):
        reviewer.apply(preview["review_id"])


@pytest.mark.parametrize("change", ["modified", "moved", "deleted"])
def test_apply_skips_stale_files_and_continues(lib_env, change):
    env = lib_env
    for name in ("first", "second"):
        make_mp3(env.library / f"{name}.mp3", tags={"title": name, "album": "Album"})
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview([track.id for track in tracks], encoded_image())
    changed = tracks[0]
    path = Path(changed.path)
    if change == "modified":
        with path.open("ab") as file:
            file.write(b"changed")
    elif change == "moved":
        moved = env.library / "moved.mp3"
        path.rename(moved)
        path = moved
        env.db.update_track_columns(changed.id, path=str(moved))
    else:
        env.db.update_track_columns(changed.id, status="deleted")
    before = path.read_bytes()
    result = reviewer.apply(preview["review_id"])
    assert len(result["applied"]) == len(result["errors"]) == 1
    assert result["errors"][0]["track_id"] == changed.id
    assert path.read_bytes() == before
    assert env.db.get_track(tracks[1].id).has_embedded_art


def test_preview_blocks_missing_outside_symlink_and_unsupported_files(lib_env):
    env = lib_env
    make_mp3(env.library / "good.mp3", tags={"title": "Song"})
    Scanner(env.config, env.db).scan()
    good = env.db.all_tracks()[0]
    outside = env.tmp / "outside.mp3"
    make_mp3(outside)
    link = env.library / "link.mp3"
    link.symlink_to(good.path)
    unsupported = env.library / "audio.aac"
    unsupported.write_bytes(b"audio")
    ids = [good.id, 99999]
    for path, fmt in ((outside, "mp3"), (link, "mp3"), (unsupported, "aac")):
        track = env.db.upsert_track(Track(
            path=str(path), format=fmt, file_hash=file_hash(str(path)),
        ))
        ids.append(track.id)
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview(ids, encoded_image())
    assert preview["ready"] == 1 and preview["blocked"] == 4
    assert all(track["reason"] for track in preview["tracks"] if track["status"] == "blocked")
    assert link.is_symlink()
    assert reviewer.apply(preview["review_id"])["errors"] == []


def test_embedding_failure_leaves_original_intact_and_cleans_temporary_copy(lib_env, monkeypatch):
    env = lib_env
    for name in ("first", "second"):
        make_mp3(env.library / f"{name}.mp3", tags={"title": name, "album": "Album"})
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    failed = tracks[0]
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview([track.id for track in tracks], encoded_image())
    before = Path(failed.path).read_bytes()
    embed = review_module.embed_cover

    def fail_one(path, data):
        if Path(path).name.startswith(f".{Path(failed.path).stem}."):
            Path(path).write_bytes(b"partial edit")
            raise PermissionError("share is read-only")
        embed(path, data)

    monkeypatch.setattr(review_module, "embed_cover", fail_one)
    result = reviewer.apply(preview["review_id"])
    assert len(result["applied"]) == len(result["errors"]) == 1
    assert "share is read-only" in result["errors"][0]["error"]
    assert Path(failed.path).read_bytes() == before
    assert env.db.get_track(failed.id).file_hash == failed.file_hash
    assert not list(env.library.glob(".*.musiclib-*"))


def test_source_changed_during_embedding_is_not_overwritten(lib_env, monkeypatch):
    env = lib_env
    path = env.library / "song.mp3"
    make_mp3(path, tags={"title": "Song", "album": "Album"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview([track.id], encoded_image())
    embed = review_module.embed_cover
    changed = path.read_bytes() + b"external change"

    def concurrent_change(temporary, data):
        path.write_bytes(changed)
        embed(temporary, data)

    monkeypatch.setattr(review_module, "embed_cover", concurrent_change)
    result = reviewer.apply(preview["review_id"])
    assert result["applied"] == []
    assert "changed while preparing" in result["errors"][0]["error"]
    assert path.read_bytes() == changed
    assert env.db.get_track(track.id).file_hash == track.file_hash
    assert not list(env.library.glob(".*.musiclib-*"))


def test_api_persists_preview_and_applies_only_saved_image_and_targets(lib_env):
    env = lib_env
    for name in ("selected", "other"):
        make_mp3(env.library / f"{name}.mp3", tags={"title": name, "artist": "Artist", "album": "Album"})
    Scanner(env.config, env.db).scan()
    selected, other = env.db.all_tracks()
    before = Path(selected.path).read_bytes(), Path(other.path).read_bytes()
    data = image_bytes()
    with TestClient(create_app(env.config)) as client:
        assert client.get("/api/stats").json()["albums_missing_art"] == 1
        response = client.post("/api/artwork/preview", json={
            "track_ids": [selected.id], "image_base64": encoded_image(data), "filename": "chosen.png",
        })
        assert response.status_code == 200
        preview_job = response.json()["job_id"]
        job = wait_job(client, preview_job)
        assert job["status"] == "done"
        summary = job["summary"]
        assert summary["ready"] == 1
        assert Path(selected.path).read_bytes() == before[0]
        assert Path(other.path).read_bytes() == before[1]
        cover = client.get(summary["cover_url"])
        assert cover.status_code == 200 and cover.content == data
        assert cover.headers["content-type"] == "image/png"
        assert client.get("/api/artwork/reviews/not-found/cover").status_code == 404
        assert client.post("/api/artwork/apply-reviewed", json={"preview_job_id": "missing"}).status_code == 400
    with TestClient(create_app(env.config)) as client:
        assert client.get(f"/api/jobs/{preview_job}").json()["status"] == "done"
        response = client.post("/api/artwork/apply-reviewed", json={
            "preview_job_id": preview_job, "image_base64": encoded_image(image_bytes(color="red")),
            "track_ids": [other.id],
        })
        assert response.status_code == 200
        result = wait_job(client, response.json()["job_id"])
        assert result["summary"] == {"applied": 1, "errors": 0}
        assert client.get("/api/stats").json()["albums_missing_art"] == 0
        assert client.get(f"/api/artwork/reviews/{summary['review_id']}").json()["status"] == "used"
        assert read_cover(selected.path) == (data, "image/png")
        assert Path(other.path).read_bytes() == before[1]
        assert client.post("/api/artwork/apply-reviewed", json={
            "preview_job_id": preview_job,
        }).status_code == 400


@pytest.mark.parametrize("status", ["suspect", "bad"])
def test_artwork_update_preserves_existing_health_findings(lib_env, status):
    env = lib_env
    make_mp3(env.library / "song.mp3", tags={"title": "Song", "artist": "Artist", "album": "Album"})
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    track.status = status
    track.quality_score = 72
    track.issues = [Issue("decode_error", "Existing health finding", "bad" if status == "bad" else "warn")]
    env.db.upsert_track(track)
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview([track.id], encoded_image())
    assert reviewer.apply(preview["review_id"])["errors"] == []
    updated = env.db.get_track(track.id)
    assert updated.status == status
    assert updated.quality_score == 72
    assert updated.issues == track.issues


def test_album_cover_is_stable_and_cache_key_changes_after_replacement(lib_env):
    env = lib_env
    old = image_bytes(fmt="JPEG", color="red")
    for name in ("z-last", "a-first"):
        make_flac(env.library / f"{name}.flac", tags={
            "TITLE": name, "ARTIST": "Artist", "ALBUM": "Album",
            "MUSICBRAINZ_ALBUMID": "catalog-release",
        }, art=old)
    Scanner(env.config, env.db).scan()
    tracks = env.db.all_tracks()
    first = tracks[0]
    with TestClient(create_app(env.config)) as client:
        before = client.get("/api/albums/page").json()["items"][0]
        assert before["cover_track_id"] == first.id
        assert client.get(before["cover_url"]).content == old
        reviewer = ArtworkReview(env.config, env.db)
        new = image_bytes()
        preview = reviewer.preview([first.id], encoded_image(new))
        assert reviewer.apply(preview["review_id"])["errors"] == []
        after = client.get("/api/albums/page").json()["items"][0]
        detail = client.get("/api/albums/detail", params={
            "album_artist": "Artist", "album": "Album",
        }).json()
        assert after["cover_url"] == detail["cover_url"] != before["cover_url"]
        assert client.get(after["cover_url"]).content == new
        assert read_cover(tracks[1].path) == (old, "image/jpeg")


@pytest.mark.parametrize("payload", ["not-base64", "", encoded_image(b"not an image"),
                                      encoded_image(image_bytes(fmt="GIF"))])
def test_invalid_image_is_reported_without_changing_files(lib_env, payload):
    env = lib_env
    path = env.library / "song.mp3"
    make_mp3(path, tags={"title": "Song"})
    Scanner(env.config, env.db).scan()
    before = path.read_bytes()
    with TestClient(create_app(env.config)) as client:
        response = client.post("/api/artwork/preview", json={
            "track_ids": [env.db.all_tracks()[0].id], "image_base64": payload,
        })
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "error" and job["error"]
        assert client.post("/api/artwork/apply-reviewed", json={
            "preview_job_id": job["id"],
        }).status_code == 400
    assert path.read_bytes() == before
    assert env.db.query_one("SELECT COUNT(*) AS n FROM artwork_reviews")["n"] == 0


def test_image_size_and_pixel_boundaries():
    png = image_bytes()
    exact_limit = png + b"\x00" * (review_module.MAX_IMAGE_BYTES - len(png))
    assert validate_cover(encoded_image(exact_limit))[1:] == ("image/png", 400, 400)
    with pytest.raises(ValueError, match="8 MiB"):
        validate_cover(encoded_image(exact_limit + b"\x00"))
    assert validate_cover(encoded_image(image_bytes(size=(5000, 5000))))[2:] == (5000, 5000)
    with pytest.raises(ValueError, match="25 million pixels"):
        validate_cover(encoded_image(image_bytes(size=(5001, 5000))))


@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png")])
def test_image_validation_decodes_the_full_uploaded_image(fmt, mime):
    data = image_bytes(fmt=fmt)
    assert validate_cover(encoded_image(data)) == (data, mime, 400, 400)
    with pytest.raises(ValueError, match="damaged|decoded"):
        validate_cover(encoded_image(data[:-30]))


def test_animated_png_is_rejected():
    output = io.BytesIO()
    with Image.new("RGB", (20, 20), "red") as first, Image.new("RGB", (20, 20), "blue") as second:
        first.save(output, format="PNG", save_all=True, append_images=[second], duration=100)
    with pytest.raises(ValueError, match="still image"):
        validate_cover(encoded_image(output.getvalue()))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for encoded audio fixtures")
@pytest.mark.parametrize("filename,codec", [
    ("audio.ogg", "libvorbis"), ("audio.opus", "libopus"), ("audio.m4a", "aac"),
    ("audio.alac.m4a", "alac"), ("audio.wav", "pcm_s16le"), ("audio.aiff", "pcm_s16be"),
])
def test_review_embeds_and_reads_back_encoded_audio_formats(lib_env, filename, codec):
    env = lib_env
    path = env.library / filename
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", codec, "-metadata", "title=Song", "-metadata", "artist=Artist",
        "-metadata", "album=Album", str(path),
    ], check=True, capture_output=True)
    old, back = image_bytes(fmt="JPEG", color="red"), image_bytes(color="green")
    audio = mutagen.File(path)
    fmt = audio.__class__.__name__
    if audio.tags is None:
        audio.add_tags()
    if fmt == "MP4":
        audio["covr"] = [
            MP4Cover(old, imageformat=MP4Cover.FORMAT_JPEG),
            MP4Cover(back, imageformat=MP4Cover.FORMAT_PNG),
        ]
    elif fmt in ("OggVorbis", "OggOpus"):
        pictures = []
        for kind, data, mime in ((3, old, "image/jpeg"), (4, back, "image/png")):
            picture = Picture()
            picture.type, picture.mime, picture.data = kind, mime, data
            pictures.append(base64.b64encode(picture.write()).decode("ascii"))
        audio["metadata_block_picture"] = pictures
    else:
        audio.tags.add(APIC(type=3, mime="image/jpeg", desc="Old", data=old))
        audio.tags.add(APIC(type=4, mime="image/png", desc="Back", data=back))
        for frame in (TIT2(text="Song"), TPE1(text="Artist"), TALB(text="Album")):
            audio.tags.add(frame)
    audio.save()
    Scanner(env.config, env.db).scan()
    track = env.db.all_tracks()[0]
    data = image_bytes(fmt="JPEG")
    reviewer = ArtworkReview(env.config, env.db)
    preview = reviewer.preview([track.id], encoded_image(data))
    assert preview["ready"] == 1
    result = reviewer.apply(preview["review_id"])
    assert len(result["applied"]) == 1 and result["errors"] == []
    assert read_cover(str(path)) == (data, "image/jpeg")
    updated = env.db.get_track(track.id)
    assert updated.has_embedded_art and updated.file_hash == file_hash(str(path))
    assert (updated.title, updated.artist, updated.album) == (track.title, track.artist, track.album)
    audio = mutagen.File(path)
    if fmt == "MP4":
        assert len(audio["covr"]) == 2 and audio["covr"][1] == back
    elif fmt in ("OggVorbis", "OggOpus"):
        pictures = [Picture(base64.b64decode(value)) for value in audio["metadata_block_picture"]]
        assert [(int(picture.type), picture.data) for picture in pictures] == [(3, data), (4, back)]
    else:
        pictures = audio.tags.getall("APIC")
        assert sorted((int(picture.type), picture.data) for picture in pictures) == [(3, data), (4, back)]
