"""SpoolLock: writers share the spool lock, the sweep holds it alone."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

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
