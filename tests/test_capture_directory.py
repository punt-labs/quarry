"""CaptureDirectory: the captures directory, reached without following a symlink."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.capture_directory import CaptureDirectory, CaptureSymlinkError


def _open(root: Path, *, create: bool) -> int:
    return CaptureDirectory(root).open(create=create)


def test_the_directory_is_created_below_the_root_when_asked(tmp_path: Path) -> None:
    fd = _open(tmp_path, create=True)
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)

    assert tmp_path.joinpath(*CaptureDirectory.RELATIVE).is_dir()


def test_a_missing_directory_is_missing_when_not_asked_to_create(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        _open(tmp_path, create=False)

    assert not (tmp_path / ".punt-labs").exists()


@pytest.mark.parametrize("level", range(len(CaptureDirectory.RELATIVE)))
def test_a_symlink_at_any_level_is_refused(tmp_path: Path, level: int) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "quarry" / "captures").mkdir(parents=True)
    path = tmp_path / "project"
    path.mkdir()
    for part in CaptureDirectory.RELATIVE[:level]:
        path = path / part
        path.mkdir()
    (path / CaptureDirectory.RELATIVE[level]).symlink_to(elsewhere)

    with pytest.raises(CaptureSymlinkError):
        _open(tmp_path / "project", create=True)


def test_a_level_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".punt-labs").write_text("not a directory")

    with pytest.raises(CaptureSymlinkError):
        _open(tmp_path, create=True)


def test_two_writers_making_the_directory_at_once_both_get_it(tmp_path: Path) -> None:
    real_mkdir = os.mkdir

    def lost_the_race(
        path: str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)  # the other writer's mkdir
        real_mkdir(path, mode, dir_fd=dir_fd)  # ... then ours: FileExistsError

    with patch("os.mkdir", lost_the_race):
        fd = _open(tmp_path, create=True)
    os.close(fd)

    assert tmp_path.joinpath(*CaptureDirectory.RELATIVE).is_dir()
