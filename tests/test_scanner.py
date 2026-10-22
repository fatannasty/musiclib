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
