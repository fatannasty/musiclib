from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomli

if sys.version_info >= (3, 11):
    import tomllib as _t11

    tomli = _t11

APP_DIR = Path.home() / ".config" / "musiclib"
DEFAULT_CONFIG_PATH = APP_DIR / "config.toml"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "musiclib" / "library.db"

CONFIG_TEMPLATE = """\
library = "{library}"
db_path = "{db_path}"
trash_dir = "{trash_dir}"

[organize]
template = "{{albumartist}}/{{album}} ({{year}})/{{track:02d}} - {{title}}.{{ext}}"
template_multidisc = "{{albumartist}}/{{album}} ({{year}})/CD{{disc}}/{{track:02d}} - {{title}}.{{ext}}"
cleanup_empty_dirs = true

[doctor]
min_bitrate = 128
min_duration = 10
deep_decode = true

[duplicates]
duration_tolerance = 5
strategy = "tags"

[artwork]
embed = true
folder_image = "folder.jpg"
min_side = 300
replace_small = false

[brainz]
contact = "you@example.com"

[sabnzbd]
url = "http://localhost:8080"
api_key = ""
complete_dir = ""

[[indexers]]
name = "indexer"
url = "https://example-indexer.com"
api_key = ""
categories = "3000"
"""


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default_config(library: str) -> Dict[str, Any]:
    return {
        "library": library,
        "db_path": str(DEFAULT_DB_PATH),
        "trash_dir": os.path.join(library, ".musiclib", "trash"),
        "organize": {
            "template": "{albumartist}/{album} ({year})/{track:02d} - {title}.{ext}",
            "template_multidisc": (
                "{albumartist}/{album} ({year})/CD{disc}/{track:02d} - {title}.{ext}"
            ),
            "cleanup_empty_dirs": True,
        },
        "doctor": {"min_bitrate": 128, "min_duration": 10, "deep_decode": True},
        "duplicates": {"duration_tolerance": 5, "strategy": "tags"},
        "artwork": {
            "embed": True,
            "folder_image": "folder.jpg",
            "min_side": 300,
            "replace_small": False,
        },
        "brainz": {"contact": "you@example.com"},
        "sabnzbd": {"url": "http://localhost:8080", "api_key": "", "complete_dir": ""},
        "indexers": [],
    }


@dataclass
class Config:
    library: str
    db_path: str
    trash_dir: str
    organize: Dict[str, Any] = field(default_factory=dict)
    doctor: Dict[str, Any] = field(default_factory=dict)
    duplicates: Dict[str, Any] = field(default_factory=dict)
    artwork: Dict[str, Any] = field(default_factory=dict)
    brainz: Dict[str, Any] = field(default_factory=dict)
    sabnzbd: Dict[str, Any] = field(default_factory=dict)
    indexers: List[Dict[str, str]] = field(default_factory=list)
    config_path: Optional[Path] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        candidates = []
        if path:
            candidates.append(Path(path))
        env_path = os.environ.get("MUSICLIB_CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path("musiclib.toml"))
        candidates.append(DEFAULT_CONFIG_PATH)

        raw: Dict[str, Any] = {}
        used: Optional[Path] = None
        for candidate in candidates:
            if candidate.is_file():
                with open(candidate, "rb") as fh:
                    raw = tomli.load(fh)
                used = candidate
                break
        if not raw:
            raise FileNotFoundError(
                "no config found; run `musiclib init --library /path/to/music` "
                f"or create one of: {', '.join(str(c) for c in candidates)}"
            )
        library = raw.get("library") or "."
        config = _deep_merge(_default_config(library), raw)
        return cls(
            library=str(Path(config["library"]).expanduser()),
            db_path=str(Path(config["db_path"]).expanduser()),
            trash_dir=str(Path(config["trash_dir"]).expanduser()),
            organize=config["organize"],
            doctor=config["doctor"],
            duplicates=config["duplicates"],
            artwork=config["artwork"],
            brainz=config["brainz"],
            sabnzbd=config["sabnzbd"],
            indexers=config.get("indexers", []),
            config_path=used,
        )

    @classmethod
    def create(cls, library: str, path: Optional[Path] = None) -> "Config":
        library = str(Path(library).expanduser().resolve())
        target = Path(path) if path else DEFAULT_CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        template = CONFIG_TEMPLATE.format(
            library=library,
            db_path=str(DEFAULT_DB_PATH),
            trash_dir=os.path.join(library, ".musiclib", "trash"),
        )
        target.write_text(template)
        return cls.load(target)

    def indexer_clients(self) -> List[Dict[str, str]]:
        return [i for i in self.indexers if i.get("url") and i.get("api_key")]
