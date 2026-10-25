"""BackfillTask: how a run ends, and what stops it."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.backfill_stats import BackfillStats
from quarry.daemon.backfill_task import BackfillTask
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.tasks import TaskState
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill import BackfillRuntime


async def _run(tmp_path: Path, stats: BackfillStats) -> TaskState:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")

    def fake(_config: object, _runtime: object) -> BackfillStats:
        return stats

    with patch("quarry.backfill.Backfill.sessions", fake):
        await BackfillTask(ctx).run(
            state, dry_run=False, collection="", project="", limit=0
        )
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

    def waits_for_stop(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        if runtime.stop.wait(timeout=10):
            saw_stop.set()
        return BackfillStats(not_attempted=3, errors=["backfill stopped: daemon"])

    with patch("quarry.backfill.Backfill.sessions", waits_for_stop):
        task = asyncio.create_task(
            BackfillTask(ctx).run(
                state, dry_run=False, collection="", project="", limit=0
            )
        )
        await asyncio.sleep(0.1)
        await ctx.ingest_queue.aclose(drain_timeout=1.0)
        await asyncio.wait_for(task, timeout=10)

    assert saw_stop.is_set()
    assert state.status == "failed"
    assert state.results["not_attempted"] == 3


async def test_a_cancelled_task_stops_the_scan_on_its_thread(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    state = TaskState(task_id="t", kind="backfill")
    saw_stop = threading.Event()

    def waits_for_stop(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        if runtime.stop.wait(timeout=10):
            saw_stop.set()
        return BackfillStats()

    with patch("quarry.backfill.Backfill.sessions", waits_for_stop):
        task = asyncio.create_task(
            BackfillTask(ctx).run(
                state, dry_run=False, collection="", project="", limit=0
            )
        )
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
        await BackfillTask(ctx).run(
            state, dry_run=False, collection="", project="", limit=0
        )

    assert state.results["ingested"] == 2
    assert isinstance(seen[0].writer, QueuedTranscriptWriter)
    assert seen[0].registry_path == ctx.settings.registry_path
