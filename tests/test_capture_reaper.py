"""CaptureReaper: only a crashed writer's leftover is reaped, failures told once."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.capture_directory import CaptureDirectory
from quarry.capture_reaper import CaptureReaper

if TYPE_CHECKING:
    import pytest

_STALE = ".session-feed0001.md.x1y2z3.tmp"


def _captures(root: Path) -> Path:
    path = root.joinpath(*CaptureDirectory.RELATIVE)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _leftover(directory: Path, name: str, *, age_s: float) -> Path:
    path = directory / name
    path.write_text("half")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def _reap(root: Path) -> int:
    return CaptureReaper(CaptureDirectory(root)).reap()


def test_a_stale_unlocked_temp_file_is_reaped(tmp_path: Path) -> None:
    stale = _leftover(_captures(tmp_path), _STALE, age_s=7200)

    assert _reap(tmp_path) == 1
    assert not stale.exists()


def test_a_young_temp_file_is_kept(tmp_path: Path) -> None:
    young = _leftover(_captures(tmp_path), _STALE, age_s=5)

    assert _reap(tmp_path) == 0
    assert young.exists()


def test_an_old_temp_file_whose_writer_is_alive_is_kept(tmp_path: Path) -> None:
    held = _leftover(_captures(tmp_path), _STALE, age_s=7200)
    fd = os.open(held, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the writer's lock

        assert _reap(tmp_path) == 0
        assert held.exists()
    finally:
        os.close(fd)

    assert _reap(tmp_path) == 1  # the writer is gone: now it is a leftover


def test_only_capture_temp_files_are_reaped(tmp_path: Path) -> None:
    directory = _captures(tmp_path)
    others = [
        _leftover(directory, "notes.tmp", age_s=7200),
        _leftover(directory, ".CLAUDE.md.x1y2.tmp", age_s=7200),
        _leftover(directory, "session-feed0001.md", age_s=7200),
    ]

    assert _reap(tmp_path) == 0
    assert all(path.exists() for path in others)


def test_a_symlink_that_looks_like_a_leftover_is_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    link = _captures(tmp_path) / _STALE
    link.symlink_to(victim)

    assert _reap(tmp_path) == 0
    assert victim.read_text() == "precious"


def test_a_missing_captures_directory_reaps_nothing(tmp_path: Path) -> None:
    assert _reap(tmp_path) == 0
    assert not (tmp_path / ".punt-labs").exists()


def test_a_symlinked_captures_directory_reaps_nothing_outside(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outside = _leftover(elsewhere, _STALE, age_s=7200)
    quarry_dir = tmp_path / "project" / ".punt-labs" / "quarry"
    quarry_dir.mkdir(parents=True)
    (quarry_dir / "captures").symlink_to(elsewhere)

    assert _reap(tmp_path / "project") == 0
    assert outside.exists()


def test_removal_failures_are_told_once_with_the_errno_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _captures(tmp_path)
    _leftover(directory, ".session-aaaa0001.md.a.tmp", age_s=7200)
    _leftover(directory, ".session-aaaa0002.md.b.tmp", age_s=7200)

    with (
        patch("os.unlink", side_effect=OSError(errno.EACCES, "denied")),
        caplog.at_level(logging.WARNING, logger="quarry.capture_reaper"),
    ):
        assert _reap(tmp_path) == 0

    [record] = caplog.records
    assert "2 leftover" in record.getMessage()
    assert "EACCES" in record.getMessage()


def test_an_unreadable_captures_directory_is_one_warning_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _captures(tmp_path)
    directory.chmod(0)
    try:
        with caplog.at_level(logging.WARNING, logger="quarry.capture_reaper"):
            assert _reap(tmp_path) == 0
    finally:
        directory.chmod(0o700)

    [record] = caplog.records
    assert "EACCES" in record.getMessage()


def test_a_project_root_that_is_a_file_is_one_warning_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("a file")

    with caplog.at_level(logging.WARNING, logger="quarry.capture_reaper"):
        assert _reap(root) == 0

    [record] = caplog.records
    assert "ENOTDIR" in record.getMessage()


def test_the_leftover_is_opened_for_writing_so_any_lock_can_be_taken(
    tmp_path: Path,
) -> None:
    _leftover(_captures(tmp_path), _STALE, age_s=7200)
    flags: list[int] = []
    real_open = os.open

    def spying(path: str, flag: int, *args: int, **kwargs: int) -> int:
        flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    with patch("quarry.capture_reaper.os.open", spying):
        assert _reap(tmp_path) == 1

    assert flags[-1] & os.O_RDWR


def test_a_filesystem_that_cannot_lock_reaps_by_age_alone(tmp_path: Path) -> None:
    old = _leftover(_captures(tmp_path), _STALE, age_s=7200)
    young = _leftover(_captures(tmp_path), ".session-feed0002.md.q.tmp", age_s=5)

    with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "no locks")):
        assert _reap(tmp_path) == 1

    assert not old.exists()
    assert young.exists()
