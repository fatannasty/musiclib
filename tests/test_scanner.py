import sqlite3
from pathlib import Path

import pytest

from musiclib.models import Album, Track
from musiclib.scanner import Scanner


def test_scan_catalogs_tracks(mini_library):
    env = mini_library
    scanner = Scanner(env.config, env.db)
    report = scanner.scan()
    assert report.scanned == 6
    assert report.errors == 1
    by_title = {}
    for track in env.db.all_tracks():
        by_title[(track.title, track.format)] = track
    first = by_title[("First Song", "flac")]
    assert first.artist == "Artist One"
    assert first.album == "Album A"
    assert first.album_artist == "Artist One"
    assert first.year == 2001
    assert first.track_no == 1
    assert first.mbid_release == "mbid-album-a"
    assert first.sample_rate == 44100
    assert first.bit_depth == 16
    assert first.duration == 12.0
    assert first.has_embedded_art is True
    assert first.file_hash and len(first.file_hash) == 64
    mp3 = by_title[("First Song", "mp3")]
    assert mp3.bitrate == 320
    assert mp3.has_embedded_art is False
    second = by_title[("Second Song", "mp3")]
    assert second.track_no == 2
    wav = by_title[(None, "wav")]
    assert wav.title is None
    assert any(i.kind == "missing_tags" for i in wav.issues)


def test_scan_marks_unparseable_as_bad(mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    bad = [t for t in env.db.all_tracks(status="bad")]
    assert len(bad) == 1
    assert bad[0].path.endswith("noise.flac")
    assert bad[0].issues[0].kind == "unparseable"


def test_scan_builds_albums(mini_library):
    env = mini_library
    Scanner(env.config, env.db).scan()
    albums = env.db.albums_all()
    assert len(albums) == 1
    album = albums[0]
    assert album.album == "Album A"
    assert album.art_status == "ok"
    assert album.have_tracks == 4


def test_incremental_rescan_skips_unchanged(mini_library):
    env = mini_library
    scanner = Scanner(env.config, env.db)
    first = scanner.scan()
    second = scanner.scan()
    assert first.skipped_unchanged == 0
    assert second.skipped_unchanged == 6
    assert second.added == 0
    assert second.updated == 0


def test_reconciliation_removes_obsolete_albums_without_touching_files(mini_library):
    env = mini_library
    scanner = Scanner(env.config, env.db, workers=1)
    scanner.scan()
    before = {track.path: Path(track.path).read_bytes() for track in env.db.all_tracks()}
    for track in env.db.all_tracks():
        if track.album:
            env.db.update_track_columns(track.id, album="Joined", album_artist="Shared artist")
    scanner.rebuild_albums()
    albums = env.db.albums_all()
    assert [(a.album_artist, a.album, a.have_tracks) for a in albums] == [
        ("Shared artist", "Joined", 4),
    ]
    assert env.db.stats()["albums"] == 1
    for track in env.db.all_tracks():
        env.db.update_track_columns(track.id, status="deleted")
    scanner.rebuild_albums()
    assert env.db.albums_all() == []
    assert env.db.stats()["albums_missing_art"] == 0
    assert all(Path(path).read_bytes() == data for path, data in before.items())


def test_reconciliation_preserves_catalog_match_and_valid_comparison_on_unchanged_scan(mini_library):
    env = mini_library
    scanner = Scanner(env.config, env.db, workers=1)
    scanner.scan()
    album = env.db.albums_all()[0]
    original_id = album.id
    album.mbid_release = "resolved-release"
    album.mbid_verified = True
    album.expected_tracks = 5
    album.missing_tracks = '[{"disc":1,"position":5,"title":"Missing"}]'
    env.db.upsert_album(album)
    scanner.scan()
    refreshed = env.db.albums_all()[0]
    assert refreshed.id == original_id
    assert refreshed.mbid_release == "resolved-release"
    assert refreshed.mbid_verified
    assert refreshed.expected_tracks == 5
    assert refreshed.missing_tracks == album.missing_tracks


@pytest.mark.parametrize("column,value", [
    ("title", "Renamed song"), ("track_no", 9), ("disc_no", 2),
    ("status", "bad"), ("mbid_release", "different-release"),
])
def test_changed_comparison_inputs_invalidate_gaps(lib_env, column, value):
    env = lib_env
    track = env.db.upsert_track(Track(
        path=str(env.library / "song.flac"), album="Album", album_artist="Artist",
        title="Song", track_no=1, disc_no=1, mbid_release="tagged-release",
    ))
    scanner = Scanner(env.config, env.db)
    scanner.rebuild_albums()
    album = env.db.albums_all()[0]
    album.mbid_release = "resolved-release"
    album.mbid_verified = True
    album.expected_tracks = 2
    album.missing_tracks = '[{"disc":1,"position":2,"title":"Missing"}]'
    env.db.upsert_album(album)
    env.db.update_track_columns(track.id, **{column: value})
    scanner.rebuild_albums()
    albums = env.db.albums_all()
    if column == "status":
        assert albums == []
        return
    refreshed = albums[0]
    assert refreshed.missing_tracks is None
    if column == "mbid_release":
        assert refreshed.mbid_release == value
        assert not refreshed.mbid_verified
        assert refreshed.expected_tracks is None
    else:
        assert refreshed.mbid_release == "resolved-release"
        assert refreshed.mbid_verified
        assert refreshed.expected_tracks == 2
    assert env.db.stats()["albums_missing_tracks"] == 0


def test_reconciliation_finds_art_in_any_member_folder_and_preserves_comparison_on_moves(lib_env):
    env = lib_env
    for folder in ("first", "second"):
        (env.library / folder).mkdir()
        env.db.upsert_track(Track(
            path=str(env.library / folder / "song.mp3"), album="Album", album_artist="Artist",
        ))
    art = env.library / "second" / "cover.jpg"
    art.write_bytes(b"folder artwork")
    scanner = Scanner(env.config, env.db)
    scanner.rebuild_albums()
    album = env.db.albums_all()[0]
    assert album.art_status == "ok"
    album.mbid_release = "resolved"
    album.mbid_verified = True
    album.missing_tracks = "[]"
    env.db.upsert_album(album)
    track = env.db.all_tracks()[0]
    env.db.update_track_columns(
        track.id, path=str(env.library / "first" / "renamed.mp3"), genre="Jazz",
        file_hash="new-hash",
    )
    scanner.rebuild_albums()
    assert env.db.albums_all()[0].missing_tracks == "[]"
    art.unlink()
    scanner.rebuild_albums()
    assert env.db.albums_all()[0].art_status == "missing"


def test_reconciliation_repairs_legacy_case_variants_unknown_artist_and_empty_catalog(lib_env):
    env = lib_env
    env.db.upsert_track(Track(path="/music/one", album="Album", album_artist="Artist"))
    env.db.upsert_track(Track(path="/music/two", album="ALBUM", album_artist="ARTIST"))
    env.db.upsert_track(Track(path="/music/unknown", album="Unnamed artist album"))
    env.db.upsert_track(Track(path="/music/bad", album="Broken", status="bad"))
    env.db.upsert_track(Track(path="/music/loose", album=""))
    env.db.upsert_album(Album(album_artist="Artist", album="Album", missing_tracks="[]"))
    env.db.upsert_album(Album(
        album_artist="ARTIST", album="ALBUM", mbid_verified=True, mbid_release="keep-me",
        expected_tracks=3, missing_tracks='[{"disc":1,"position":3,"title":"Missing"}]',
    ))
    env.db.upsert_album(Album(album_artist="Obsolete", album="Gone"))
    Scanner(env.config, env.db).rebuild_albums()
    albums = env.db.albums_all()
    assert len(albums) == 2
    matched = next(a for a in albums if a.album.lower() == "album")
    assert (matched.have_tracks, matched.mbid_release, matched.mbid_verified) == (2, "keep-me", True)
    assert matched.missing_tracks is None
    assert matched.expected_tracks == 3
    assert next(a for a in albums if a.album == "Unnamed artist album").album_artist == "Unknown Artist"
    assert env.db.stats()["albums"] == 2


def test_reconciliation_is_atomic_on_database_failure(lib_env):
    env = lib_env
    env.db.upsert_track(Track(path="/music/one", album="One", album_artist="Artist"))
    env.db.upsert_track(Track(path="/music/two", album="Two", album_artist="Artist"))
    scanner = Scanner(env.config, env.db)
    scanner.rebuild_albums()
    before = [dict(row) for row in env.db.query("SELECT * FROM albums ORDER BY id")]
    for track in env.db.all_tracks():
        env.db.update_track_columns(track.id, title="Changed")
    env.db.execute(
        "CREATE TRIGGER fail_album_update BEFORE UPDATE ON albums WHEN OLD.album='Two' "
        "BEGIN SELECT RAISE(ABORT, 'album storage unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="album storage unavailable"):
        scanner.rebuild_albums()
    assert [dict(row) for row in env.db.query("SELECT * FROM albums ORDER BY id")] == before
    assert env.db.albums_dirty
