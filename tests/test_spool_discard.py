"""SpoolDiscard: a spool record of a job that stored is removed, best effort."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.daemon.abort_reason import Aborted
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.spool_discard import SpoolDiscard

if TYPE_CHECKING:
    import pytest


def _discard() -> SpoolDiscard:
    return SpoolDiscard(DaemonThreads(2, "discard-test"))


async def _until_gone(path: Path) -> None:
    for _ in range(200):
        if not path.exists():
            return
        await asyncio.sleep(0.01)
    msg = f"{path} was never removed"
    raise AssertionError(msg)


async def test_a_record_is_removed_off_the_loop(tmp_path: Path) -> None:
    record = tmp_path / "remember-1.json"
    record.write_text("{}")

    _discard().discard(record)

    await _until_gone(record)


async def test_no_path_and_a_missing_file_are_not_errors(tmp_path: Path) -> None:
    discard = _discard()

    discard.discard(None)
    discard.discard(tmp_path / "never-written.json")
    await asyncio.sleep(0.05)


async def test_a_removal_that_fails_is_logged_and_never_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record = tmp_path / "remember-2.json"
    record.write_text("{}")

    with (
        patch.object(Path, "unlink", side_effect=PermissionError("secret path")),
        caplog.at_level(logging.WARNING, logger="quarry.daemon"),
    ):
        _discard().discard(record)
        await asyncio.sleep(0.1)

    assert "could not remove the spool record" in caplog.text
    assert "secret path" not in caplog.text
    assert record.exists()


async def test_a_late_spool_that_wrote_a_record_has_it_removed(
    tmp_path: Path,
) -> None:
    record = tmp_path / "remember-3.json"
    record.write_text("{}")
    spooling: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    spooling.set_result(Aborted("spooled", path=record))

    _discard().discard_late(spooling)

    await _until_gone(record)


async def test_a_late_spool_that_failed_or_was_cancelled_removes_nothing(
    tmp_path: Path,
) -> None:
    record = tmp_path / "remember-4.json"
    record.write_text("{}")
    failed: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    failed.set_exception(OSError("disk"))
    cancelled: asyncio.Future[Aborted] = asyncio.get_running_loop().create_future()
    cancelled.cancel()

    discard = _discard()
    discard.discard_late(failed)
    discard.discard_late(cancelled)
    await asyncio.sleep(0.05)

    assert record.exists()
