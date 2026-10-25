"""WatchStop: the teardown order of a started watch loop, and how it reports."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from quarry.daemon.watch_stop import WatchStop

if TYPE_CHECKING:
    import pytest

    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.watch_roster import WatchRoster


def _roster(events: list[str]) -> WatchRoster:
    return cast(
        "WatchRoster",
        SimpleNamespace(
            unwatch_all=lambda: events.append("unwatch"),
            close=lambda: events.append("close"),
        ),
    )


async def test_the_steps_run_in_order_and_the_observer_is_joined_last() -> None:
    events: list[str] = []
    source = SimpleNamespace(stop=lambda: events.append("join"))

    await WatchStop(
        _roster(events), cast("FsEventSource", source), None, None, None
    ).run()

    assert events == ["unwatch", "close", "join"]


async def test_a_crashed_safety_task_is_named_when_it_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def crashes() -> None:
        msg = "secret"
        raise RuntimeError(msg)

    task = asyncio.ensure_future(crashes())
    task.add_done_callback(WatchStop.safety_ended)

    with caplog.at_level(logging.ERROR, logger="quarry.daemon.watch_stop"):
        await asyncio.wait({task})
        await asyncio.sleep(0)

    assert "safety reconcile loop crashed" in caplog.text
    assert "secret" not in caplog.text


async def test_a_cancelled_safety_task_is_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = asyncio.ensure_future(asyncio.sleep(10))
    task.add_done_callback(WatchStop.safety_ended)
    task.cancel()

    with caplog.at_level(logging.ERROR, logger="quarry.daemon.watch_stop"):
        await asyncio.wait({task})
        await asyncio.sleep(0)

    assert caplog.text == ""
