"""SpoolLock: writers share the spool lock, the sweep holds it alone."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.daemon.spool_lock import SpoolLock

if TYPE_CHECKING:
    from pathlib import Path


def test_two_shared_holders_do_not_wait_for_each_other(tmp_path: Path) -> None:
    lock = SpoolLock(tmp_path)

    with lock.shared(), lock.shared():
        assert (tmp_path / SpoolLock.NAME).exists()


def test_the_exclusive_holder_waits_for_a_shared_one_and_then_gets_it(
    tmp_path: Path,
) -> None:
    lock = SpoolLock(tmp_path)
    got: list[float] = []

    def sweep() -> None:
        with lock.exclusive():
            got.append(time.monotonic())

    with lock.shared():
        thread = threading.Thread(target=sweep, daemon=True)
        thread.start()
        time.sleep(0.3)
        assert got == []  # waiting for the writer
        released = time.monotonic()
    thread.join(5)

    assert got
    assert got[0] >= released


def test_a_waiter_says_it_is_waiting_before_it_blocks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lock = SpoolLock(tmp_path)
    done = threading.Event()

    def sweep() -> None:
        with lock.exclusive():
            done.set()

    with caplog.at_level(logging.INFO, logger="quarry.daemon"), lock.shared():
        thread = threading.Thread(target=sweep, daemon=True)
        thread.start()
        time.sleep(0.3)
        assert f"waiting for the lock .lock in {tmp_path}" in caplog.text
        assert not done.is_set()
    thread.join(5)


def test_an_uncontended_lock_is_taken_without_saying_anything(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="quarry.daemon"),
        SpoolLock(tmp_path).exclusive(),
    ):
        pass

    assert "waiting for the lock" not in caplog.text


def test_the_lock_is_tried_without_blocking_first(tmp_path: Path) -> None:
    modes: list[int] = []
    real_flock = fcntl.flock

    def spy(fd: int, mode: int) -> None:
        modes.append(mode)
        real_flock(fd, mode)

    with (
        patch("quarry.daemon.spool_lock.fcntl.flock", spy),
        SpoolLock(tmp_path).shared(),
    ):
        pass

    assert modes == [fcntl.LOCK_SH | fcntl.LOCK_NB]


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_a_filesystem_without_flock_is_warned_about_once_and_works_unlocked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    ran: list[str] = []

    with (
        patch.object(SpoolLock, "_unsupported_warned", new=False),
        patch(
            "quarry.daemon.spool_lock.fcntl.flock",
            side_effect=OSError(code, "no locks here"),
        ),
        caplog.at_level(logging.WARNING, logger="quarry.daemon"),
    ):
        with SpoolLock(tmp_path).shared():
            ran.append("write")
        with SpoolLock(tmp_path).exclusive():
            ran.append("sweep")

    assert ran == ["write", "sweep"]
    warnings = [r for r in caplog.records if "does not support flock" in r.getMessage()]
    assert len(warnings) == 1


def test_a_lock_error_that_is_not_lack_of_support_propagates_and_closes_the_fd(
    tmp_path: Path,
) -> None:
    fds: list[int] = []
    real_open = os.open

    def spy_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        fds.append(fd)
        return fd

    with (
        patch("quarry.daemon.spool_lock.os.open", spy_open),
        patch(
            "quarry.daemon.spool_lock.fcntl.flock",
            side_effect=OSError(errno.EIO, "disk"),
        ),
        pytest.raises(OSError, match="disk"),
        SpoolLock(tmp_path).shared(),
    ):
        pytest.fail("the body must not run without the lock")

    assert len(fds) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fds[0])
