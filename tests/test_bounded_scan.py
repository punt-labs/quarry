"""BoundedScan: a scan on a thread is awaited through every cancel, but not for good."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass, field

import pytest

from quarry.daemon.bounded_scan import BoundedScan
from quarry.daemon.late_outcome import ScanStillRunningError
from quarry.daemon.tasks import TaskState
from quarry.stop_signal import StopSignal


@dataclass(slots=True)
class _Report:
    """A scan report that remembers what it was told, and says 'partial' so far."""

    recorded: list[str] = field(default_factory=list)
    late_results: list[str] = field(default_factory=list)

    def record(self, result: str) -> None:
        self.recorded.append(result)

    def late(self, result: str) -> None:
        self.late_results.append(result)

    def so_far(self) -> str:
        return "partial"


def _state() -> TaskState:
    return TaskState(task_id="t", kind="backfill")


def _held(state: TaskState) -> bool:
    return state.holds_thread


async def _started(scan_started: threading.Event) -> None:
    assert await asyncio.to_thread(scan_started.wait, 5)


async def test_a_scan_that_returns_has_its_result_recorded_and_returned() -> None:
    report = _Report()

    result = await BoundedScan(StopSignal(), 5.0).run(lambda: "done", _state(), report)

    assert result == "done"
    assert report.recorded == ["done"]


async def test_a_double_cancel_still_waits_for_the_thread() -> None:
    stop = StopSignal()
    started, release = threading.Event(), threading.Event()
    report = _Report()

    def lingers() -> str:
        started.set()
        release.wait(timeout=10)  # finishing the transcript it was on
        return "done"

    task = asyncio.create_task(BoundedScan(stop, 30.0).run(lingers, _state(), report))
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
    assert report.recorded == ["done"]  # the result survives the cancel


async def test_the_first_stop_reason_is_the_one_a_cancelled_scan_keeps() -> None:
    stop = StopSignal()
    stop.set("daemon shutting down")
    state = _state()
    report = _Report()
    started, release = threading.Event(), threading.Event()

    def lingers() -> str:
        started.set()
        release.wait(timeout=10)
        return stop.reason

    task = asyncio.create_task(BoundedScan(stop, 30.0).run(lingers, state, report))
    await _started(started)
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)

    assert report.recorded == ["daemon shutting down"]
    assert state.error == "backfill stopped (daemon shutting down)"


async def test_a_scan_that_raised_propagates_and_is_not_logged_here(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> str:
        msg = "/secret/path/session.jsonl"
        raise RuntimeError(msg)

    report = _Report()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await BoundedScan(StopSignal(), 5.0).run(boom, _state(), report)

    assert report.recorded == []
    assert caplog.text == ""  # the task's terminal records it, once


async def test_a_cancelled_scan_that_raised_is_logged_by_class_and_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started, release = threading.Event(), threading.Event()

    def fails_late() -> str:
        started.set()
        release.wait(timeout=10)
        msg = "/secret/path"
        raise OSError(msg)

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 30.0).run(fails_late, _state(), _Report())
    )
    await _started(started)
    task.cancel()
    release.set()
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await task

    assert "OSError" in caplog.text
    assert "fails_late" in caplog.text
    assert "/secret/path" not in caplog.text


async def test_the_thread_is_held_against_the_guard_until_it_returns() -> None:
    state = _state()
    started, release = threading.Event(), threading.Event()

    def holds() -> str:
        started.set()
        release.wait(timeout=10)
        return "done"

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 30.0).run(holds, state, _Report())
    )
    await _started(started)

    assert state.holds_thread

    release.set()
    await task
    assert not state.holds_thread


async def test_a_thread_that_will_not_stop_is_given_up_on_with_what_it_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = StopSignal()
    state = _state()
    report = _Report()
    started, release = threading.Event(), threading.Event()

    def blocked() -> str:
        started.set()
        release.wait(timeout=10)  # a read the stop cannot interrupt
        return "finally"

    task = asyncio.create_task(BoundedScan(stop, 0.3).run(blocked, state, report))
    await _started(started)
    stop.set("daemon shutting down")
    began = time.monotonic()

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(ScanStillRunningError) as raised,
    ):
        await task

    assert 0.2 < time.monotonic() - began < 3.0
    expected = "backfill stopped (daemon shutting down); scan thread still running"
    assert str(raised.value) == expected
    assert state.error == expected
    assert report.recorded == ["partial"]  # what the scan had counted
    assert "daemon shutting down" in caplog.text
    assert "still running" in caplog.text
    assert _held(state)  # the guard still refuses a second run

    release.set()
    await asyncio.sleep(0.3)
    assert not _held(state)
    assert report.late_results == ["finally"]  # the late outcome is written


async def test_a_cancelled_scan_given_up_on_still_ends_cancelled_with_its_reason() -> (
    None
):
    state = _state()
    report = _Report()
    started, release = threading.Event(), threading.Event()

    def blocked() -> str:
        started.set()
        release.wait(timeout=10)
        return "finally"

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 0.3).run(blocked, state, report)
    )
    await _started(started)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert report.recorded == ["partial"]
    assert state.error == (
        "backfill stopped (task cancelled); scan thread still running"
    )
    assert _held(state)
    release.set()
    await asyncio.sleep(0.3)
    assert not _held(state)


async def test_a_late_failure_is_logged_by_class_and_never_left_unretrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = StopSignal()
    state = _state()
    started, release = threading.Event(), threading.Event()

    def blocked() -> str:
        started.set()
        release.wait(timeout=10)
        msg = "/secret/path"
        raise OSError(msg)

    task = asyncio.create_task(BoundedScan(stop, 0.3).run(blocked, state, _Report()))
    await _started(started)
    stop.set("daemon shutting down")
    with pytest.raises(ScanStillRunningError):
        await task

    with caplog.at_level(logging.ERROR):
        release.set()
        await asyncio.sleep(0.3)
        gc.collect()

    assert "OSError" in caplog.text
    assert "blocked" in caplog.text
    assert "/secret/path" not in caplog.text
    assert "never retrieved" not in caplog.text
