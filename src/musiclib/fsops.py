from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def trash_file(path: str, trash_root: str) -> str:
    src = Path(path)
    month_dir = Path(trash_root) / datetime.now().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(month_dir / src.name)
    shutil.move(str(src), str(dest))
    return str(dest)


def delete_file(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def remove_empty_dirs(root: str) -> int:
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        rel = os.path.relpath(dirpath, root)
        if rel.split(os.sep)[0].startswith("."):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass
    return removed


def is_inside(path: str, root: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def case_insensitive_exists(directory: Path, filename: str) -> Optional[Path]:
    if not directory.exists():
        return None
    lowered = filename.lower()
    for entry in directory.iterdir():
        if entry.name.lower() == lowered:
            return entry
    return None
