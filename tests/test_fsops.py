import os

from musiclib.fsops import (
    case_insensitive_exists,
    delete_file,
    is_inside,
    remove_empty_dirs,
    trash_file,
    unique_path,
)


def test_trash_file_moves_with_unique_names(tmp_path):
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    trash = tmp_path / "trash"
    first = trash_file(str(src), str(trash))
    assert not src.exists()
    assert os.path.basename(first) == "a.mp3"
    src2 = tmp_path / "a.mp3"
    src2.write_bytes(b"y")
    second = trash_file(str(src2), str(trash))
    assert os.path.basename(second) == "a (2).mp3"
    assert os.path.dirname(first) == os.path.dirname(second)


def test_unique_path_increments(tmp_path):
    target = tmp_path / "file.txt"
    assert unique_path(target) == target
    target.write_text("x")
    assert unique_path(target) == tmp_path / "file (2).txt"
    (tmp_path / "file (2).txt").write_text("y")
    assert unique_path(target) == tmp_path / "file (3).txt"


def test_remove_empty_dirs_only_empty(tmp_path):
    (tmp_path / "empty1" / "empty2").mkdir(parents=True)
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "song.mp3").write_bytes(b"z")
    removed = remove_empty_dirs(str(tmp_path))
    assert removed == 2
    assert keep.exists()
    assert not (tmp_path / "empty1").exists()


def test_is_inside(tmp_path):
    inside = str(tmp_path / "a" / "b.mp3")
    assert is_inside(inside, str(tmp_path)) is True
    assert is_inside("/elsewhere/b.mp3", str(tmp_path)) is False


def test_delete_and_case_insensitive_exists(tmp_path):
    f = tmp_path / "Song.Mp3"
    f.write_bytes(b"1")
    assert case_insensitive_exists(tmp_path, "song.mp3") == f
    delete_file(str(f))
    assert not f.exists()
    delete_file(str(f))
    assert case_insensitive_exists(tmp_path, "song.mp3") is None
