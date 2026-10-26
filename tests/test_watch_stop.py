"""WatchStop: the teardown order of a started watch loop, and how it reports."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from quarry.daemon.watch_stop import WatchStop
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.daemon.debounce import DebouncedDispatcher
    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter


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


class _Timers:
    """A dispatcher or submitter stand-in that notes it was told to cancel."""

    def __init__(self) -> None:
        self.cancelled = 0

    def cancel_all(self) -> None:
        self.cancelled += 1

    def cancel_pending(self) -> None:
        self.cancelled += 1


async def test_a_cancel_during_the_safety_retire_still_cancels_timers_and_closes() -> (
    None
):
    events: list[str] = []
    release = asyncio.Event()

    async def slow_to_retire() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue

    safety = asyncio.ensure_future(slow_to_retire())
    timers = _Timers()
    stopping = asyncio.create_task(
        WatchStop(
            _roster(events),
            None,
            safety,
            cast("WatchSubmitter", timers),
            cast("DebouncedDispatcher", timers),
        ).run()
    )
    await asyncio.sleep(0.1)  # in _retire_safety
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert timers.cancelled == 2  # the submitter's retries and the debounce timers
    assert events == ["unwatch", "close"]
    release.set()
    await safety


async def test_every_cleanup_runs_even_when_an_earlier_one_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    roster = _roster(events)

    class _Broken:
        def cancel_pending(self) -> None:
            msg = "retries stuck"
            raise RuntimeError(msg)

    with (
        caplog.at_level(logging.ERROR, logger="quarry.daemon.watch_stop"),
        pytest.raises(RuntimeError),
    ):
        await WatchStop(
            roster, None, None, cast("WatchSubmitter", _Broken()), None
        ).run()

    assert events == ["unwatch", "close"]  # the later steps still ran
    assert caplog.text.count("cancelling the submitter's retries failed") == 1


async def test_a_logged_failure_is_marked_so_the_boundary_does_not_log_it_again() -> (
    None
):
    class _Broken:
        def cancel_pending(self) -> None:
            msg = "retries stuck"
            raise RuntimeError(msg)

    try:
        await WatchStop(
            _roster([]), None, None, cast("WatchSubmitter", _Broken()), None
        ).run()
    except RuntimeError as exc:
        assert RedactedFailure.is_logged(exc)
    else:
        raise AssertionError("the failure was not raised")


async def test_an_abandoned_join_that_fails_later_is_logged_not_left_unread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()

    def stop_that_fails() -> None:
        release.wait(timeout=10)
        msg = "late failure detail"
        raise RuntimeError(msg)

    source = cast("FsEventSource", SimpleNamespace(stop=stop_that_fails))
    with caplog.at_level(logging.INFO):
        stopping = asyncio.create_task(
            WatchStop(_roster([]), source, None, None, None).run()
        )
        await asyncio.sleep(0.1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        release.set()
        await asyncio.sleep(0.3)
        gc.collect()

    assert "observer join failed after the stop was cancelled" in caplog.text
    assert "late failure detail" not in caplog.text
    assert "never retrieved" not in caplog.text


async def test_an_abandoned_join_that_succeeds_later_says_it_joined_after_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    source = cast("FsEventSource", SimpleNamespace(stop=lambda: release.wait(10)))
    with caplog.at_level(logging.INFO, logger="quarry.daemon.watch_stop"):
        stopping = asyncio.create_task(
            WatchStop(_roster([]), source, None, None, None).run()
        )
        await asyncio.sleep(0.1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        release.set()
        await asyncio.sleep(0.3)

    assert "the observer joined after all" in caplog.text
