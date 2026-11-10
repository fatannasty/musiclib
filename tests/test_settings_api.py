from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from musiclib.config import Config  # noqa: E402
from musiclib.server.app import create_app  # noqa: E402


def test_settings_roundtrip_masks_and_keeps_secrets(lib_env):
    client = TestClient(create_app(lib_env.config))
    got = client.get("/api/settings").json()
    assert got["sabnzbd"]["api_key"] == ""

    body = {
        "sabnzbd": {"url": "http://nas:8080", "api_key": "SECRET", "complete_dir": "/dl"},
        "brainz": {"contact": "me@example.com"},
        "catalog": {"album_source": "itunes", "ipv4_only": True},
        "doctor": {"min_bitrate": "192", "deep_decode": False},
        "indexers": [{"name": "idx", "url": "https://i.example", "api_key": "K1", "categories": "3000"}],
    }
    assert client.put("/api/settings", json=body).json()["saved"] is True

    got = client.get("/api/settings").json()
    assert got["sabnzbd"]["api_key_set"] is True
    assert got["sabnzbd"]["api_key"] == ""
    assert got["indexers"][0]["api_key_set"] is True

    blank = {"sabnzbd": {"url": "http://nas:9090", "api_key": ""},
             "indexers": [{"name": "idx", "url": "https://i.example", "api_key": ""}]}
    assert client.put("/api/settings", json=blank).status_code == 200

    reloaded = Config.load(lib_env.config.config_path)
    assert reloaded.sabnzbd["api_key"] == "SECRET"
    assert reloaded.sabnzbd["url"] == "http://nas:9090"
    assert reloaded.indexers[0]["api_key"] == "K1"
    assert reloaded.doctor["min_bitrate"] == 192
    assert reloaded.doctor["deep_decode"] is False
    assert reloaded.organize["template"].startswith("{albumartist}")
    assert reloaded.catalog == {"album_source": "itunes", "ipv4_only": True}


def test_settings_rejects_bad_number(lib_env):
    client = TestClient(create_app(lib_env.config))
    assert client.put("/api/settings", json={"doctor": {"min_bitrate": "abc"}}).status_code == 400


def test_settings_rejects_unknown_catalog_without_applying_other_changes(lib_env):
    with TestClient(create_app(lib_env.config)) as client:
        before = client.get("/api/settings").json()
        response = client.put("/api/settings", json={
            "catalog": {"album_source": "unknown"}, "doctor": {"min_bitrate": 192},
        })
        assert response.status_code == 400
        after = client.get("/api/settings").json()
    assert after["catalog"] == before["catalog"]
    assert after["doctor"] == before["doctor"]


def test_existing_config_without_catalog_section_gets_compatible_defaults(lib_env):
    path = lib_env.tmp / "old-config.toml"
    path.write_text(f'library = "{lib_env.library}"\ndb_path = "{lib_env.config.db_path}"\n')
    config = Config.load(path)
    with TestClient(create_app(config)) as client:
        settings = client.get("/api/settings").json()
        assert settings["catalog"] == {"album_source": "all", "ipv4_only": False}
        assert client.put("/api/settings", json={
            "catalog": {"album_source": "library", "ipv4_only": True},
        }).status_code == 200
    assert Config.load(path).catalog == {"album_source": "library", "ipv4_only": True}
