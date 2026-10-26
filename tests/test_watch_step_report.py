"""StepReport: what is said of a cleanup step the stop did not wait out."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from quarry.daemon.watch_step_report import StepReport

if TYPE_CHECKING:
    import pytest


async def test_a_clean_end_in_the_cancel_tick_says_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    done.set_result(None)

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        StepReport.abandon("step", done, None)

    assert caplog.records == []


async def test_a_step_still_running_is_named_with_the_right_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    running: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        StepReport.abandon("first", running, 0.5)
        StepReport.abandon("second", running, 0.0)

    assert "stopped while first was in progress" in caplog.text
    assert "second was started after the grace ran out and is not awaited" in (
        caplog.text
    )
    running.cancel()
    await asyncio.sleep(0)
    assert "first was cancelled after the stop gave up on it" in caplog.text
