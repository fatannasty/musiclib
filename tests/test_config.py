
from musiclib.config import Config


def test_create_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("musiclib.config.DEFAULT_DB_PATH", tmp_path / "d" / "lib.db")
    library = tmp_path / "Music"
    library.mkdir()
    config_path = tmp_path / "config.toml"
    config = Config.create(str(library), path=config_path)
    assert config.config_path == config_path
    assert config.library == str(library.resolve())
    assert config.db_path == str(tmp_path / "d" / "lib.db")
    assert config.duplicates["strategy"] == "tags"
    loaded = Config.load(config_path)
    assert loaded.library == config.library
    assert loaded.organize["template"] == config.organize["template"]


def test_indexer_clients_filters_incomplete(lib_env):
    config = lib_env.config
    config.indexers = [
        {"name": "good", "url": "https://idx.example", "api_key": "k"},
        {"name": "nokey", "url": "https://idx2.example", "api_key": ""},
    ]
    clients = config.indexer_clients()
    assert len(clients) == 1
    assert clients[0]["name"] == "good"
