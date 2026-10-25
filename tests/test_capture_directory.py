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


def test_the_captures_directory_is_owner_only_and_the_levels_above_are_not(
    tmp_path: Path,
) -> None:
    old = os.umask(0)  # so the mode asked for is the mode made
    try:
        os.close(_open(tmp_path, create=True))
    finally:
        os.umask(old)

    def mode(depth: int) -> int:
        path = tmp_path.joinpath(*CaptureDirectory.RELATIVE[:depth])
        return stat.S_IMODE(path.stat().st_mode)

    assert [mode(1), mode(2), mode(3)] == [0o755, 0o755, 0o700]


def test_a_level_that_is_a_file_says_it_is_a_non_directory(tmp_path: Path) -> None:
    (tmp_path / ".punt-labs").write_text("not a directory")

    with pytest.raises(CaptureSymlinkError, match="non-directory in the path"):
        _open(tmp_path, create=True)


def test_a_symlink_planted_after_the_mkdir_is_refused_not_followed(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real_mkdir = os.mkdir

    def planted(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)
        os.rmdir(path, dir_fd=dir_fd)
        os.symlink(elsewhere, path, dir_fd=dir_fd)  # swapped in before the reopen

    with (
        patch("os.mkdir", planted),
        pytest.raises(CaptureSymlinkError, match="in the path"),
    ):
        _open(tmp_path, create=True)


def test_a_level_that_is_a_file_where_the_create_races_is_refused(
    tmp_path: Path,
) -> None:
    real_mkdir = os.mkdir

    def file_won_the_race(
        path: str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=dir_fd)
        os.close(fd)
        real_mkdir(path, mode, dir_fd=dir_fd)  # FileExistsError: a file is there

    with (
        patch("os.mkdir", file_won_the_race),
        pytest.raises(CaptureSymlinkError, match="non-directory in the path"),
    ):
        _open(tmp_path, create=True)
