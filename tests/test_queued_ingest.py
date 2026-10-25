"""QueuedIngest: a job through its collection's FIFO, awaited, never registered."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, final

import pytest

from quarry.daemon.queued_ingest import IngestNotAdmittedError, QueuedIngest
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState, task_terminal
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord

_COLLECTION = "proj-captures"


@final
@dataclass(slots=True)
class _Unit:
    """A job that logs when it starts and ends, and can wait, block or fail."""

    name: str
    events: list[tuple[str, str]]
    collection: str = _COLLECTION
    delay: float = 0.0
    gate: asyncio.Event | None = None
    boom: bool = False
    spool: SpoolRecord | None = field(default=None)

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Log the start, wait as configured, log the end, record the outcome."""
        del ctx
        with task_terminal(state):
            self.events.append(("start", self.name))
            try:
                if self.gate is not None:
                    await self.gate.wait()
                await asyncio.sleep(self.delay)
                if self.boom:
                    msg = "stub failure"
                    raise RuntimeError(msg)
                state.status = "completed"
            finally:
                self.events.append(("end", self.name))

    def spool_record(self) -> SpoolRecord | None:
        """Return no record: nothing here is spooled."""
        return self.spool


def _hook_submit(ctx: DaemonContext, unit: _Unit) -> None:
    """Admit *unit* the way a hook's capture route does."""
    state = TaskState(task_id=unit.name, kind="capture")
    assert ctx.ingest_queue.try_submit(RouteKey("db", unit.collection), unit, state)


async def test_a_backfill_job_and_hook_jobs_for_one_collection_never_interleave(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    events: list[tuple[str, str]] = []
    _hook_submit(ctx, _Unit("hook-1", events, delay=0.05))
    backfill = asyncio.create_task(
        QueuedIngest(ctx).run(_Unit("backfill", events, delay=0.05))
    )
    await asyncio.sleep(0.01)  # the backfill job is admitted behind hook-1
    _hook_submit(ctx, _Unit("hook-2", events, delay=0.05))

    state = await backfill
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert state.status == "completed"
    assert events == [
        ("start", "hook-1"),
        ("end", "hook-1"),
        ("start", "backfill"),
        ("end", "backfill"),
        ("start", "hook-2"),
        ("end", "hook-2"),
    ]


async def test_a_full_queue_that_never_drains_refuses_the_job(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path, depth=1)
    events: list[tuple[str, str]] = []
    gate = asyncio.Event()
    _hook_submit(ctx, _Unit("hook", events, gate=gate))

    with pytest.raises(IngestNotAdmittedError, match="not admitted in time"):
        await QueuedIngest(ctx, admit_timeout_s=0.2).run(_Unit("backfill", events))

    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)
    assert ("start", "backfill") not in events


async def test_a_full_queue_that_drains_in_time_admits_the_job(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path, depth=1)
    events: list[tuple[str, str]] = []
    _hook_submit(ctx, _Unit("hook", events, delay=0.15))

    state = await QueuedIngest(ctx, admit_timeout_s=5.0).run(_Unit("backfill", events))
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert state.status == "completed"
    assert events[-2:] == [("start", "backfill"), ("end", "backfill")]


async def test_a_job_that_fails_is_returned_failed_with_its_reason(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)

    state = await QueuedIngest(ctx).run(_Unit("backfill", [], boom=True))
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert state.status == "failed"
    assert state.error == "internal error: RuntimeError"


async def test_queued_jobs_leave_no_entry_in_the_task_registry(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    queued = QueuedIngest(ctx)

    for i in range(3):
        await queued.run(_Unit(f"backfill-{i}", []))
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert len(ctx.tasks) == 0
