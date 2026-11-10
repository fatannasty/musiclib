import sqlite3

from musiclib.db import Database
from musiclib.models import Album, Issue, Track


def test_upsert_insert_then_update(lib_env):
    db = lib_env.db
    track = Track(path="/x/a.flac", title="T", album="AL", artist="AR", size=10, mtime=1.0)
    db.upsert_track(track)
    assert track.id is not None
    first_id = track.id
    track.title = "T2"
    db.upsert_track(track)
    assert track.id == first_id
    fetched = db.get_track(first_id)
    assert fetched.title == "T2"


def test_issues_replace(lib_env):
    db = lib_env.db
    track = Track(path="/x/a.flac")
    db.upsert_track(track)
    track.issues = [Issue("low_bitrate", "64kbps", "warn")]
    db.replace_issues(track)
    rows = db.query("SELECT * FROM issues WHERE track_id=?", (track.id,))
    assert len(rows) == 1
    track.issues = []
    db.replace_issues(track)
    rows = db.query("SELECT * FROM issues WHERE track_id=?", (track.id,))
    assert len(rows) == 0


def test_album_upsert_and_query(lib_env):
    db = lib_env.db
    album = Album(album_artist="AR", album="AL", have_tracks=5)
    db.upsert_album(album)
    assert album.id is not None
    album.have_tracks = 6
    db.upsert_album(album)
    albums = db.albums_all()
    assert len(albums) == 1
    assert albums[0].have_tracks == 6


def test_stats_and_deletions(lib_env):
    db = lib_env.db
    db.upsert_track(Track(path="/x/a.flac", size=100, duration=60, has_embedded_art=True))
    db.upsert_track(Track(path="/x/b.flac", size=200, duration=60))
    stats = db.stats()
    assert stats["tracks"] == 2
    assert stats["bytes"] == 300
    assert stats["with_art"] == 1
    db.record_deletion("/x/b.flac", 200, "h", "test", "/trash/b.flac")
    rows = db.query("SELECT * FROM deletions")
    assert len(rows) == 1
    assert rows[0]["reason"] == "test"


def test_album_cache_migration_preserves_existing_catalog_and_is_repeatable(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, album_artist TEXT NOT NULL, "
            "album TEXT NOT NULL, year INTEGER, mbid_release TEXT, mbid_verified INTEGER DEFAULT 0, "
            "art_status TEXT DEFAULT 'unknown', expected_tracks INTEGER, have_tracks INTEGER DEFAULT 0, "
            "missing_tracks TEXT, UNIQUE(album_artist,album))"
        )
        connection.execute(
            "INSERT INTO albums (album_artist,album,mbid_release,mbid_verified,expected_tracks) "
            "VALUES ('Artist','Album','resolved',1,12)"
        )
    for _ in range(2):
        db = Database(path)
        try:
            album = db.albums_all()[0]
            assert (album.mbid_release, album.mbid_verified, album.expected_tracks) == ("resolved", True, 12)
            row = db.query_one("SELECT track_signature,release_signature FROM albums")
            assert row["track_signature"] is row["release_signature"] is None
        finally:
            db.close()
