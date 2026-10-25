"""BackfillTask: how a run ends, and what stops it."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.backfill_stats import BackfillStats
from quarry.daemon.backfill_args import BackfillArgs
from quarry.daemon.backfill_task import BackfillTask
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.task_terminal import TaskTerminal
from quarry.daemon.tasks import TaskState
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill import BackfillRuntime
    from quarry.stop_signal import StopSignal


def _waited_for(stop: StopSignal, seconds: float = 10.0) -> bool:
    """Poll *stop* the way the scan does, for at most *seconds*."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop.is_set():
            return True
        time.sleep(0.01)
    return False


_ARGS = BackfillArgs(dry_run=False, collection="", project="", limit=0)


async def _run(tmp_path: Path, stats: BackfillStats) -> TaskState:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")

    def fake(_config: object, _runtime: object) -> BackfillStats:
        return stats

    with patch("quarry.backfill.Backfill.sessions", fake):
        await BackfillTask(ctx).run(state, _ARGS)
    return state


async def test_a_run_that_stored_nothing_and_had_errors_ends_failed(
    tmp_path: Path,
) -> None:
    state = await _run(
        tmp_path, BackfillStats(failed=2, errors=["a.jsonl: boom", "b.jsonl: boom"])
    )

    assert state.status == "failed"
    assert state.error == "backfill stored nothing: a.jsonl: boom"
    assert state.results["failed"] == 2  # the stats are still reported


async def test_a_run_that_stored_something_ends_completed_despite_errors(
    tmp_path: Path,
) -> None:
    state = await _run(tmp_path, BackfillStats(ingested=1, failed=1, errors=["x: y"]))

    assert state.status == "completed"
    assert state.error == ""


async def test_a_run_with_nothing_to_do_ends_completed(tmp_path: Path) -> None:
    state = await _run(tmp_path, BackfillStats(skipped_existing=4))

    assert state.status == "completed"


async def test_a_queue_that_closes_stops_the_scan_on_its_thread(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")
    saw_stop = threading.Event()
    reasons: list[str] = []

    def waits_for_stop(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        if _waited_for(runtime.stop):
            saw_stop.set()
        reasons.append(runtime.stop.reason)
        return BackfillStats(
            not_attempted=3,
            stopped=runtime.stop.reason,
            errors=[f"backfill stopped: {runtime.stop.reason}"],
        )

    with patch("quarry.backfill.Backfill.sessions", waits_for_stop):
        task = asyncio.create_task(BackfillTask(ctx).run(state, _ARGS))
        await asyncio.sleep(0.1)
        await ctx.ingest_queue.aclose(drain_timeout=1.0)
        await asyncio.wait_for(task, timeout=10)

    assert saw_stop.is_set()
    assert reasons == ["daemon shutting down"]
    assert state.status == "failed"
    assert state.error == (
        "backfill stored nothing: backfill stopped: daemon shutting down"
    )
    assert state.results["not_attempted"] == 3


async def test_a_cancelled_task_stops_the_scan_on_its_thread(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")
    saw_stop = threading.Event()

    def waits_for_stop(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        if _waited_for(runtime.stop):
            saw_stop.set()
        return BackfillStats()

    with patch("quarry.backfill.Backfill.sessions", waits_for_stop):
        task = asyncio.create_task(BackfillTask(ctx).run(state, _ARGS))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(saw_stop.wait, 10)


async def test_the_run_writes_through_the_fifo_over_the_daemons_registry(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    seen: list[BackfillRuntime] = []

    def records_the_runtime(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        seen.append(runtime)
        return BackfillStats(ingested=2)

    with patch("quarry.backfill.Backfill.sessions", records_the_runtime):
        state = TaskState(task_id="t", kind="backfill")
        await BackfillTask(ctx).run(state, _ARGS)

    assert state.results["ingested"] == 2
    assert isinstance(seen[0].writer, QueuedTranscriptWriter)
    assert seen[0].registry_path == ctx.settings.registry_path


async def test_a_cancelled_task_gives_its_reason_to_the_scan(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")
    reasons: list[str] = []

    def records_the_reason(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        _waited_for(runtime.stop)
        reasons.append(runtime.stop.reason)
        return BackfillStats()

    with patch("quarry.backfill.Backfill.sessions", records_the_reason):
        task = asyncio.create_task(BackfillTask(ctx).run(state, _ARGS))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert reasons == ["task cancelled"]


async def test_a_run_that_stopped_but_stored_something_is_completed_and_says_so(
    tmp_path: Path,
) -> None:
    stats = BackfillStats(
        ingested=1,
        not_attempted=2,
        stopped="task cancelled",
        errors=["backfill stopped: task cancelled"],
    )

    state = await _run(tmp_path, stats)

    assert state.status == "completed"
    assert state.results["stopped"] is True
    assert state.results["stop_reason"] == "task cancelled"


async def test_a_cancelled_run_is_not_over_until_its_thread_returns(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")
    release = threading.Event()
    finished = threading.Event()

    def lingers_after_the_stop(
        _config: object, runtime: BackfillRuntime
    ) -> BackfillStats:
        _waited_for(runtime.stop)
        release.wait(timeout=10)  # still finishing the transcript it was on
        finished.set()
        return BackfillStats()

    with patch("quarry.backfill.Backfill.sessions", lingers_after_the_stop):
        task = asyncio.create_task(BackfillTask(ctx).run(state, _ARGS))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.3)

        # The registry's task ends when this one does, so the singleton guard
        # still sees a running backfill while the thread is alive.
        assert not task.done()
        assert not finished.is_set()
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert finished.is_set()
    assert task.cancelled()


async def test_a_cancelled_run_reports_the_reason_it_really_stopped_for(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")

    def stops_for_the_shutdown(
        _config: object, runtime: BackfillRuntime
    ) -> BackfillStats:
        _waited_for(runtime.stop)
        return BackfillStats(
            not_attempted=1,
            stopped=runtime.stop.reason,
            errors=[f"backfill stopped: {runtime.stop.reason}"],
        )

    with patch("quarry.backfill.Backfill.sessions", stops_for_the_shutdown):
        task = asyncio.create_task(BackfillTask(ctx).run(state, _ARGS))
        await asyncio.sleep(0.1)
        await ctx.ingest_queue.aclose(drain_timeout=1.0)  # the daemon is stopping
        task.cancel()  # ... and then the task is cancelled
        await asyncio.gather(task, return_exceptions=True)

    assert state.results["stop_reason"] == "daemon shutting down"


async def test_a_double_cancel_does_not_abandon_the_scan_thread(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")
    release = threading.Event()

    def lingers(_config: object, _runtime: BackfillRuntime) -> BackfillStats:
        release.wait(timeout=10)
        return BackfillStats()

    with patch("quarry.backfill.Backfill.sessions", lingers):
        task = asyncio.create_task(BackfillTask(ctx).run(state, _ARGS))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.3)

        assert not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()


async def test_a_thread_that_will_not_stop_keeps_the_guard_shut_after_the_bound(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = ctx.tasks.begin("backfill")
    release = threading.Event()

    def blocked(_config: object, _runtime: BackfillRuntime) -> BackfillStats:
        release.wait(timeout=10)  # a read the stop cannot interrupt
        return BackfillStats()

    with patch("quarry.backfill.Backfill.sessions", blocked):

        async def as_the_route_runs_it() -> None:
            with TaskTerminal.recording(state):
                await BackfillTask(ctx, bound_s=0.3).run(state, _ARGS)

        task = asyncio.create_task(as_the_route_runs_it())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()  # the loop is not held for the hung thread
        assert ctx.tasks.running_of_kind("backfill") is state  # the guard still is
        assert state.error == (
            "backfill stopped (task cancelled); scan thread still running"
        )
        assert state.results["stop_reason"] == "task cancelled"  # what it had counted
        release.set()
        await asyncio.sleep(0.3)

    assert ctx.tasks.running_of_kind("backfill") is None
