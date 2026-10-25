"""BoundedScan: a scan on a thread is awaited through every cancel, but not for good."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time

import pytest

from quarry.backfill_stats import BackfillStats
from quarry.daemon.bounded_scan import BoundedScan
from quarry.daemon.late_outcome import ScanStillRunningError
from quarry.daemon.run_record import RunRecord
from quarry.daemon.tasks import TaskState
from quarry.stop_signal import StopSignal


def _state() -> TaskState:
    return TaskState(task_id="t", kind="backfill")


def _record(state: TaskState) -> RunRecord:
    return RunRecord(state, StopSignal(), dry_run=False)


def _stats(ingested: int = 1) -> BackfillStats:
    return BackfillStats(ingested=ingested)


def _ingested(state: TaskState) -> object:
    return state.results["ingested"]


def _held(state: TaskState) -> bool:
    return state.holds_thread


async def _started(scan_started: threading.Event) -> None:
    assert await asyncio.to_thread(scan_started.wait, 5)


async def test_a_scan_that_returns_has_its_result_recorded_and_returned() -> None:
    state = _state()

    result = await BoundedScan(StopSignal(), 5.0).run(_stats, state, _record(state))

    assert result == _stats()
    assert (state.status, state.results["ingested"]) == ("completed", 1)


async def test_a_double_cancel_still_waits_for_the_thread() -> None:
    stop = StopSignal()
    started, release = threading.Event(), threading.Event()
    state = _state()

    def lingers() -> BackfillStats:
        started.set()
        release.wait(timeout=10)  # finishing the transcript it was on
        return _stats()

    task = asyncio.create_task(
        BoundedScan(stop, 30.0).run(lingers, state, _record(state))
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
    assert state.results["ingested"] == 1  # the result survives the cancel


async def test_the_first_stop_reason_is_the_one_a_cancelled_scan_keeps() -> None:
    stop = StopSignal()
    stop.set("daemon shutting down")
    state = _state()
    started, release = threading.Event(), threading.Event()

    def lingers() -> BackfillStats:
        started.set()
        release.wait(timeout=10)
        return BackfillStats(stopped=stop.reason)

    task = asyncio.create_task(
        BoundedScan(stop, 30.0).run(lingers, state, _record(state))
    )
    await _started(started)
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)

    assert state.results["stop_reason"] == "daemon shutting down"
    assert state.error == "backfill stopped (daemon shutting down)"


async def test_a_scan_that_raised_propagates_and_is_not_logged_here(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> BackfillStats:
        msg = "/secret/path/session.jsonl"
        raise RuntimeError(msg)

    state = _state()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        await BoundedScan(StopSignal(), 5.0).run(boom, state, _record(state))

    assert state.results == {}
    assert caplog.text == ""  # the task's terminal records it, once


async def test_a_cancelled_scan_that_raised_is_logged_by_class_and_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started, release = threading.Event(), threading.Event()
    state = _state()

    def fails_late() -> BackfillStats:
        started.set()
        release.wait(timeout=10)
        msg = "/secret/path"
        raise OSError(msg)

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 30.0).run(fails_late, state, _record(state))
    )
    await _started(started)
    task.cancel()
    release.set()
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await task

    assert "OSError" in caplog.text
    assert "fails_late" in caplog.text
    assert "/secret/path" not in caplog.text
    # the cancel hides the failure from the caller: the task's error names it
    assert state.error == "backfill stopped (task cancelled); scan failed: OSError"


async def test_the_thread_is_held_against_the_guard_until_it_returns() -> None:
    state = _state()
    started, release = threading.Event(), threading.Event()

    def holds() -> BackfillStats:
        started.set()
        release.wait(timeout=10)
        return _stats()

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 30.0).run(holds, state, _record(state))
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
    started, release = threading.Event(), threading.Event()

    def blocked() -> BackfillStats:
        started.set()
        release.wait(timeout=10)  # a read the stop cannot interrupt
        return _stats(7)

    task = asyncio.create_task(
        BoundedScan(stop, 0.3).run(blocked, state, _record(state))
    )
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
    assert _ingested(state) == 0  # what the scan had counted
    assert "daemon shutting down" in caplog.text
    assert "still running" in caplog.text
    assert _held(state)  # the guard still refuses a second run

    release.set()
    await asyncio.sleep(0.3)
    assert not _held(state)
    assert _ingested(state) == 7  # the late outcome is written
    assert state.error == (
        "backfill stopped (daemon shutting down); scan thread returned late: "
        "7 ingested, 0 skipped, 0 failed, 0 not attempted"
    )


async def test_a_cancelled_scan_given_up_on_still_ends_cancelled_with_its_reason() -> (
    None
):
    state = _state()
    started, release = threading.Event(), threading.Event()

    def blocked() -> BackfillStats:
        started.set()
        release.wait(timeout=10)
        return _stats()

    task = asyncio.create_task(
        BoundedScan(StopSignal(), 0.3).run(blocked, state, _record(state))
    )
    await _started(started)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.results["ingested"] == 0
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

    def blocked() -> BackfillStats:
        started.set()
        release.wait(timeout=10)
        msg = "/secret/path"
        raise OSError(msg)

    task = asyncio.create_task(
        BoundedScan(stop, 0.3).run(blocked, state, _record(state))
    )
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
    # the error no longer says "still running": it says how the thread ended
    assert state.error == (
        "backfill stopped (daemon shutting down); scan thread failed late: OSError"
    )
