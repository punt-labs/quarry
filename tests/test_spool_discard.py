"""SpoolDiscard: a spool record of a job that stored is removed, best effort."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.daemon.abort_reason import Aborted
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.spool_discard import SpoolDiscard

if TYPE_CHECKING:
    import pytest


def _refuse(path: Path) -> None:
    del path
    msg = "secret path"
    raise PermissionError(msg)


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


async def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    discard = _discard()

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

    assert "could not remove the spool record remember-2.json" in caplog.text
    assert "may duplicate stored content" in caplog.text
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


async def test_a_removal_is_pending_until_it_ends_and_drain_waits_for_it(
    tmp_path: Path,
) -> None:
    record = tmp_path / "remember-5.json"
    record.write_text("{}")
    discard = _discard()

    discard.discard(record)
    during = discard.pending
    left = await discard.drain(5.0)

    assert during == (record,)
    assert left == ()
    assert discard.pending == ()
    assert not record.exists()


async def test_drain_names_a_removal_that_outlasts_its_timeout(tmp_path: Path) -> None:
    record = tmp_path / "remember-6.json"
    record.write_text("{}")
    release = threading.Event()
    discard = _discard()
    with patch.object(
        SpoolDiscard, "_unlink", staticmethod(lambda _path: release.wait(5))
    ):
        discard.discard(record)
        left = await discard.drain(0.05)
    release.set()

    assert left == (record,)
    await discard.drain(5.0)


async def test_a_removal_that_raised_is_logged_by_name_without_its_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record = tmp_path / "remember-7.json"
    with (
        patch.object(SpoolDiscard, "_unlink", staticmethod(_refuse)),
        caplog.at_level(logging.ERROR, logger="quarry.daemon"),
    ):
        discard = _discard()
        discard.discard(record)
        await discard.drain(5.0)

    assert "removing the spool record remember-7.json failed" in caplog.text
    assert "secret path" not in caplog.text


async def test_a_removal_is_synced_to_its_directory(tmp_path: Path) -> None:
    record = tmp_path / "remember-8.json"
    record.write_text("{}")

    with patch("quarry.daemon.spool_discard.PrivateWrite.sync_directory") as sync:
        discard = _discard()
        discard.discard(record)
        await discard.drain(5.0)

    sync.assert_called_once_with(tmp_path, "spool record remember-8.json removed")
