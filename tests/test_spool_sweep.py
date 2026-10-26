"""StartupSweep: the spool sweep runs in the background and says how it ended."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from quarry.daemon.job_spool import JobSpool
from quarry.daemon.spool_sweep import StartupSweep

if TYPE_CHECKING:
    import pytest


class _Spool:
    """A spool double whose sweep does what the test says."""

    def __init__(self, sweep: object) -> None:
        self.sweep = sweep


async def test_a_sweep_that_never_returns_does_not_delay_the_caller() -> None:
    release = threading.Event()
    spool = cast("JobSpool", _Spool(lambda: release.wait(10)))

    started = time.monotonic()
    task = StartupSweep.start(spool)
    await asyncio.sleep(0)  # the caller goes on at once
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert not task.done()
    release.set()
    await task


async def test_the_end_of_the_sweep_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool = JobSpool(tmp_path / "spool")

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        await StartupSweep.start(spool)
        await asyncio.sleep(0.05)

    assert "the startup sweep finished" in caplog.text


async def test_a_failing_sweep_is_logged_not_left_unread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> None:
        msg = "disk detail"
        raise OSError(msg)

    spool = cast("JobSpool", _Spool(boom))

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        task = StartupSweep.start(spool)
        await asyncio.wait({task})
        await asyncio.sleep(0.05)

    assert "the startup sweep failed" in caplog.text
    assert "disk detail" not in caplog.text
