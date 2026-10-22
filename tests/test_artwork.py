import os

from conftest import ALBUM_A, PNG_500, JPEG_600x400, make_flac

from musiclib.artwork import Artwork, image_dimensions, image_mime
from musiclib.scanner import Scanner


def test_image_dimensions_and_mime():
    assert image_dimensions(PNG_500) == (500, 500)
    assert image_dimensions(JPEG_600x400) == (600, 400)
    assert image_mime(PNG_500) == "image/png"
    assert image_mime(JPEG_600x400) == "image/jpeg"
    assert image_dimensions(b"garbage") is None


def _album_with_tracks(env, n=2, art=False):
    for i in range(1, n + 1):
        make_flac(
            env.library / "Artist One" / "Album A" / f"t{i}.flac",
            tags={**ALBUM_A, "TITLE": f"Track {i}", "TRACKNUMBER": str(i)},
            art=JPEG_600x400 if art else None,
        )
    Scanner(env.config, env.db).scan()


def test_process_album_downloads_and_embeds(lib_env, monkeypatch):
    env = lib_env
    _album_with_tracks(env, art=False)
    artwork = Artwork(env.config, env.db)
    monkeypatch.setattr(
        artwork, "download_front", lambda mbid: JPEG_600x400 if mbid else None
    )
    album = env.db.albums_all()[0]
    result = artwork.process_album(album, lambda a, b: ("mbid-xyz", 2001, 1))
    assert result.status == "downloaded"
    assert result.embedded == 2
    album_dir = os.path.join(env.config.library, "Artist One", "Album A")
    assert os.path.exists(os.path.join(album_dir, "folder.jpg"))
    for track in env.db.all_tracks():
        assert track.has_embedded_art is True
    refreshed = env.db.albums_all()[0]
    assert refreshed.art_status == "downloaded"
    assert refreshed.mbid_release == "mbid-xyz"
    assert refreshed.mbid_verified is True


def test_process_album_no_musicbrainz_match(lib_env, monkeypatch):
    env = lib_env
    _album_with_tracks(env)
    artwork = Artwork(env.config, env.db)
    monkeypatch.setattr(artwork, "download_front", lambda mbid: None)
    album = env.db.albums_all()[0]
    result = artwork.process_album(album, lambda a, b: None)
    assert result.status == "none"
    assert env.db.albums_all()[0].art_status == "none"


def test_process_album_rejects_small_art(lib_env, monkeypatch):
    env = lib_env
    small = JPEG_600x400[:7] + (100).to_bytes(2, "big") + (90).to_bytes(2, "big") + JPEG_600x400[11:]
    _album_with_tracks(env)
    artwork = Artwork(env.config, env.db)
    monkeypatch.setattr(artwork, "download_front", lambda mbid: small)
    album = env.db.albums_all()[0]
    result = artwork.process_album(album, lambda a, b: ("mbid", 2001, 1))
    assert result.status == "small"
    assert env.db.albums_all()[0].art_status == "small"
