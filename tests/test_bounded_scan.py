"""BoundedScan: a scan on a thread is awaited through every cancel, but not for good."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from quarry.daemon.bounded_scan import BoundedScan, ScanStillRunningError
from quarry.daemon.tasks import TaskState
from quarry.stop_signal import StopSignal


def _state() -> TaskState:
    return TaskState(task_id="t", kind="backfill")


async def _started(scan_started: threading.Event) -> None:
    assert await asyncio.to_thread(scan_started.wait, 5)


async def test_a_scan_that_returns_has_its_result_recorded_and_returned() -> None:
    recorded: list[int] = []

    result = await BoundedScan(StopSignal(), 5.0).run(
        lambda: 7, _state(), recorded.append
    )

    assert result == 7
    assert recorded == [7]


async def test_a_double_cancel_still_waits_for_the_thread() -> None:
    stop = StopSignal()
    started, release = threading.Event(), threading.Event()
    recorded: list[str] = []

    def lingers() -> str:
        started.set()
        release.wait(timeout=10)  # finishing the transcript it was on
        return "done"

    task = asyncio.create_task(
        BoundedScan(stop, 30.0).run(lingers, _state(), recorded.append)
    )
    await _started(started)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # the second cancel must not abandon the wait
    await asyncio.sleep(0.2)

    assert not task.done()
    assert stop.reason == "task cancelled"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded == ["done"]  # the result survives the cancel


async def test_the_first_stop_reason_is_the_one_a_cancelled_scan_keeps() -> None:
    stop = StopSignal()
    stop.set("daemon shutting down")
    recorded: list[str] = []

    task = asyncio.create_task(
        BoundedScan(stop, 30.0).run(lambda: stop.reason, _state(), recorded.append)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert recorded == ["daemon shutting down"]


async def test_a_scan_that_raised_is_logged_by_class_and_frames_not_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> None:
        msg = "/secret/path/session.jsonl"
        raise RuntimeError(msg)

    recorded: list[None] = []
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await BoundedScan(StopSignal(), 5.0).run(boom, _state(), recorded.append)

    assert recorded == []
    assert "RuntimeError" in caplog.text
    assert "boom" in caplog.text  # the frame names the code
    assert "/secret/path" not in caplog.text


async def test_a_cancelled_scan_that_raised_logs_it_and_still_cancels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started, release = threading.Event(), threading.Event()

    def fails_late() -> None:
        started.set()
        release.wait(timeout=10)
        msg = "late"
        raise OSError(msg)

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 30.0).run(fails_late, _state(), print)
    )
    await _started(started)
    task.cancel()
    release.set()
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await task

    assert "OSError" in caplog.text


async def test_the_thread_is_held_against_the_guard_until_it_returns() -> None:
    state = _state()
    started, release = threading.Event(), threading.Event()

    def holds() -> None:
        started.set()
        release.wait(timeout=10)

    task = asyncio.create_task(BoundedScan(StopSignal(), 30.0).run(holds, state, print))
    await _started(started)

    assert state.holds_thread

    release.set()
    await task
    assert not state.holds_thread


async def test_a_thread_that_will_not_stop_is_given_up_on_after_the_bound() -> None:
    stop = StopSignal()
    state = _state()
    started, release = threading.Event(), threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=10)  # a read the stop cannot interrupt

    task = asyncio.create_task(BoundedScan(stop, 0.3).run(blocked, state, print))
    await _started(started)
    stop.set("daemon shutting down")
    began = time.monotonic()

    with pytest.raises(ScanStillRunningError):
        await task

    assert 0.2 < time.monotonic() - began < 3.0
    assert state.holds_thread  # the guard still refuses a second run
    release.set()
    await asyncio.sleep(0.2)
    assert not state.holds_thread


async def test_a_cancelled_scan_given_up_on_still_ends_cancelled() -> None:
    state = _state()
    started, release = threading.Event(), threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=10)

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 0.3).run(blocked, state, print)
    )
    await _started(started)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.holds_thread
    release.set()
    await asyncio.sleep(0.2)
    assert not state.holds_thread
