from __future__ import annotations

import errno
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def atomic_edit(path: Path, edit: Callable[[Path], T]) -> T:
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}.musiclib-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        shutil.copy2(path, temporary)
        result = edit(temporary)
        os.replace(temporary, path)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def move_no_replace(
    source: str, target: str, verify: Optional[Callable[[str], None]] = None,
) -> None:
    source_stat = os.stat(source, follow_symlinks=False)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    created: Optional[os.stat_result] = None
    removed = False

    def owns_destination() -> bool:
        if created is None:
            return False
        try:
            current = os.stat(target, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)

    try:
        try:
            os.link(source, target, follow_symlinks=False)
            created = source_stat
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK):
                raise
            # NAS mounts may forbid links even when copying and removing files is allowed.
            with open(source, "rb") as src, open(target, "xb") as dest:
                created = os.fstat(dest.fileno())
                shutil.copyfileobj(src, dest)
                dest.flush()
                os.fsync(dest.fileno())
            if not owns_destination():
                raise OSError("Destination changed during the move. The source was not removed.") from None
            shutil.copystat(source, target)
        if verify is not None:
            verify(target)
        if not owns_destination():
            raise OSError("Destination changed during the move. The source was not removed.")
        os.unlink(source)
        removed = True
    finally:
        if not removed and owns_destination():
            os.unlink(target)


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
