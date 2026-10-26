"""WatchStop: the teardown order of a started watch loop, and how it reports."""

from __future__ import annotations

import asyncio
import gc
import itertools
import logging
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.daemon.watch_stop import StepCancelledError, WatchStop
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.daemon.debounce import DebouncedDispatcher
    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter


_GRACE_S = 0.2  # what a WatchStop built directly gets after a cancel
_DRAIN_TIMEOUT_S = 30.0  # the teardown budget for_loop takes the grace from


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

    await WatchStop.for_loop(
        _roster(events),
        cast("FsEventSource", source),
        None,
        None,
        None,
        _DRAIN_TIMEOUT_S,
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

    with caplog.at_level(logging.ERROR, logger="quarry.daemon"):
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

    with caplog.at_level(logging.ERROR, logger="quarry.daemon"):
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
        WatchStop.for_loop(
            _roster(events),
            None,
            safety,
            cast("WatchSubmitter", timers),
            cast("DebouncedDispatcher", timers),
            _DRAIN_TIMEOUT_S,
        ).run()
    )
    await asyncio.sleep(0.1)  # in the safety retire
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert timers.cancelled == 2  # the submitter's retries and the debounce timers
    assert events == ["unwatch", "close"]
    release.set()
    await safety


async def test_a_step_failing_during_a_cancel_is_logged_and_the_cancel_still_wins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    release = asyncio.Event()

    async def slow_to_retire() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue

    class _Broken:
        def cancel_pending(self) -> None:
            msg = "retries stuck"
            raise RuntimeError(msg)

    safety = asyncio.ensure_future(slow_to_retire())
    stopping = asyncio.create_task(
        WatchStop.for_loop(
            _roster(events),
            None,
            safety,
            cast("WatchSubmitter", _Broken()),
            None,
            _DRAIN_TIMEOUT_S,
        ).run()
    )
    await asyncio.sleep(0.1)  # in _retire_safety
    with caplog.at_level(logging.ERROR, logger="quarry.daemon"):
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping

    assert caplog.text.count("cancelling the submitter's retries failed") == 1
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
        caplog.at_level(logging.ERROR, logger="quarry.daemon"),
        pytest.raises(RuntimeError),
    ):
        await WatchStop.for_loop(
            roster,
            None,
            None,
            cast("WatchSubmitter", _Broken()),
            None,
            _DRAIN_TIMEOUT_S,
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
        await WatchStop.for_loop(
            _roster([]),
            None,
            None,
            cast("WatchSubmitter", _Broken()),
            None,
            _DRAIN_TIMEOUT_S,
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
            WatchStop.for_loop(
                _roster([]), source, None, None, None, _DRAIN_TIMEOUT_S
            ).run()
        )
        await asyncio.sleep(0.1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        release.set()
        await asyncio.sleep(0.3)
        gc.collect()

    assert "joining the observer failed after the stop gave up on it" in caplog.text
    assert "late failure detail" not in caplog.text
    assert "never retrieved" not in caplog.text


async def test_an_abandoned_join_that_succeeds_later_says_it_joined_after_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    source = cast("FsEventSource", SimpleNamespace(stop=lambda: release.wait(10)))
    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        stopping = asyncio.create_task(
            WatchStop.for_loop(
                _roster([]), source, None, None, None, _DRAIN_TIMEOUT_S
            ).run()
        )
        await asyncio.sleep(0.1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        release.set()
        await asyncio.sleep(0.3)

    assert "joining the observer finished after the stop gave up on it" in caplog.text


async def test_an_unexpected_exception_from_the_safety_retire_still_runs_every_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    safety = asyncio.ensure_future(asyncio.sleep(60))
    timers = _Timers()

    async def exploding(_task: asyncio.Task[None]) -> None:
        msg = "retire exploded"
        raise ValueError(msg)

    with patch.object(WatchStop, "retire", exploding):
        stop = WatchStop.for_loop(
            _roster(events),
            None,
            safety,
            cast("WatchSubmitter", timers),
            cast("DebouncedDispatcher", timers),
            _DRAIN_TIMEOUT_S,
        )
    with (
        caplog.at_level(logging.ERROR, logger="quarry.daemon"),
        pytest.raises(ValueError, match="retire exploded"),
    ):
        await stop.run()

    assert timers.cancelled == 2
    assert events == ["unwatch", "close"]
    assert caplog.text.count("retiring the safety task failed") == 1
    safety.cancel()


async def test_a_step_that_raises_while_a_cancel_is_pending_never_replaces_it() -> None:
    events: list[str] = []
    release = asyncio.Event()

    async def slow() -> None:
        await release.wait()

    async def raises() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    async def last() -> None:
        events.append("last")

    running = asyncio.create_task(
        WatchStop([("slow", slow), ("raises", raises), ("last", last)], _GRACE_S).run()
    )
    await asyncio.sleep(0.05)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    assert events == ["last"]
    release.set()


async def test_after_a_cancel_each_step_waits_only_within_the_grace_and_in_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    release = asyncio.Event()

    async def hangs() -> None:
        await release.wait()

    async def hangs_too() -> None:
        await release.wait()

    async def quick() -> None:
        events.append("quick")

    stop = WatchStop(
        [("first", hangs), ("second", hangs_too), ("third", quick)], grace_s=0.2
    )
    running = asyncio.create_task(stop.run())
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        elapsed = asyncio.get_running_loop().time() - started
        release.set()
        await asyncio.sleep(0.05)

    assert elapsed < 0.2 + 0.15  # the grace, with scheduling slack, not unbounded
    assert events == ["quick"]  # the later step still ran
    assert "stopped while second was in progress" in caplog.text
    assert "second finished after the stop gave up on it" in caplog.text


class _Fatal(BaseException):
    """A BaseException that is not a cancel and does not end the loop."""


async def test_a_base_exception_from_a_step_lets_the_rest_run_then_is_raised() -> None:
    events: list[str] = []

    async def fatal() -> None:
        raise _Fatal

    async def last() -> None:
        events.append("last")

    with pytest.raises(_Fatal):
        await WatchStop([("fatal", fatal), ("last", last)], _GRACE_S).run()

    assert events == ["last"]


async def test_a_blocking_roster_step_does_not_block_the_loop_past_the_grace() -> None:
    ticks: list[float] = []
    loop = asyncio.get_running_loop()
    release = threading.Event()

    def blocking_close() -> None:
        release.wait(2)

    roster = cast(
        "WatchRoster",
        SimpleNamespace(unwatch_all=lambda: None, close=blocking_close),
    )

    async def heartbeat() -> None:
        while True:
            ticks.append(loop.time())
            await asyncio.sleep(0.02)

    beat = asyncio.create_task(heartbeat())
    stopping = asyncio.create_task(
        WatchStop.for_loop(roster, None, None, None, None, _DRAIN_TIMEOUT_S).run()
    )
    await asyncio.sleep(0.3)
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    release.set()
    await stopping
    beat.cancel()

    assert max(gaps) < 0.2  # the loop kept ticking while close blocked


async def test_a_steps_own_cancel_is_a_failure_not_the_teardowns_cancel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    async def cancels_itself() -> None:
        raise asyncio.CancelledError

    async def last() -> None:
        events.append("last")

    with (
        caplog.at_level(logging.ERROR, logger="quarry.daemon"),
        pytest.raises(StepCancelledError),
    ):
        await WatchStop([("itself", cancels_itself), ("last", last)], _GRACE_S).run()

    assert events == ["last"]
    assert "itself failed" in caplog.text


async def test_a_step_with_no_grace_left_is_reported_as_not_awaited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    release = asyncio.Event()

    async def hangs() -> None:
        await release.wait()

    async def last() -> None:
        await asyncio.sleep(0.05)
        events.append("last")

    running = asyncio.create_task(
        WatchStop([("first", hangs), ("second", hangs), ("third", last)], 0.0).run()
    )
    await asyncio.sleep(0.05)
    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.1)

    assert "second was started after the grace ran out and is not awaited" in (
        caplog.text
    )
    assert "second was in progress" not in caplog.text
    release.set()


async def test_a_step_that_finishes_in_the_cancel_tick_is_not_logged_as_both(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def finishes() -> None:
        await asyncio.sleep(0)

    running = asyncio.create_task(WatchStop([("quick", finishes)], _GRACE_S).run())
    await asyncio.sleep(0)  # the stop starts the step
    await asyncio.sleep(0)  # the step runs to its end; the stop has not woken yet
    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.05)

    assert "quick was in progress" not in caplog.text
    assert "finished after the stop gave up" not in caplog.text


async def test_a_cancelled_late_step_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def slow_then_cancelled() -> None:
        await asyncio.sleep(0.15)
        raise asyncio.CancelledError

    running = asyncio.create_task(WatchStop([("slow", slow_then_cancelled)], 0.0).run())
    await asyncio.sleep(0.02)
    with caplog.at_level(logging.INFO, logger="quarry.daemon"):
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.3)

    assert "slow was cancelled after the stop gave up on it" in caplog.text


def test_a_close_that_never_returns_does_not_keep_the_process_alive() -> None:
    """anyio's worker threads are not daemon threads; these are."""
    script = textwrap.dedent(
        """
        import asyncio, threading
        from types import SimpleNamespace
        from quarry.daemon.watch_stop import WatchStop

        never = threading.Event()
        roster = SimpleNamespace(unwatch_all=lambda: None, close=never.wait)

        async def main():
            stop = WatchStop.for_loop(roster, None, None, None, None, 5.0)
            try:
                await asyncio.wait_for(stop.run(), 0.3)
            except (TimeoutError, asyncio.CancelledError):
                pass

        asyncio.run(main())
        """
    )
    started = time.monotonic()

    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=30, check=False
    )

    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started < 15


async def test_the_roster_steps_and_the_observer_join_never_run_at_once() -> None:
    running = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def step() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(0.2)
        with lock:
            running -= 1

    roster = cast("WatchRoster", SimpleNamespace(unwatch_all=step, close=step))
    source = cast("FsEventSource", SimpleNamespace(stop=step))
    stopping = asyncio.create_task(
        WatchStop.for_loop(roster, source, None, None, None, _DRAIN_TIMEOUT_S).run()
    )
    await asyncio.sleep(0.05)
    stopping.cancel()  # give up on the first step; the others still queue behind it
    with pytest.raises(asyncio.CancelledError):
        await stopping
    release.set()
    await asyncio.sleep(0.5)

    assert peak == 1
