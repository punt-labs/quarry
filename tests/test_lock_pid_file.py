"""Tests for quarry.lock_pid_file: the pid written into a held lockfile."""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.lock_pid_file import LockPidFile

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def lockfile(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    path = tmp_path / "lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR)
    yield descriptor, path
    os.close(descriptor)


def test_the_pid_is_written(lockfile: tuple[int, Path]) -> None:
    fd, path = lockfile
    LockPidFile(fd).record(4242)
    assert path.read_text() == "4242"


def test_a_longer_earlier_value_is_replaced(lockfile: tuple[int, Path]) -> None:
    fd, path = lockfile
    path.write_text("1234567890")
    LockPidFile(fd).record(7)
    assert path.read_text() == "7"


def test_a_short_write_is_retried_until_the_whole_pid_lands(
    lockfile: tuple[int, Path],
) -> None:
    fd, path = lockfile
    real = os.pwrite
    with patch("os.pwrite", side_effect=lambda f, data, off: real(f, data[:1], off)):
        LockPidFile(fd).record(4242)
    assert path.read_text() == "4242"


def test_a_write_that_makes_no_progress_is_an_eio_error(
    lockfile: tuple[int, Path],
) -> None:
    fd, _ = lockfile
    with patch("os.pwrite", return_value=0), pytest.raises(OSError, match="short") as e:
        LockPidFile(fd).record(4242)
    assert e.value.errno == errno.EIO


def test_a_second_record_replaces_a_longer_first_exactly(
    lockfile: tuple[int, Path],
) -> None:
    fd, path = lockfile
    record = LockPidFile(fd)
    record.record(1234567)
    record.record(89)
    assert path.read_text() == "89"
