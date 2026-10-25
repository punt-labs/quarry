"""QueuedIngest: a job through its collection's FIFO, awaited, never registered."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, final

import pytest

from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError
from quarry.daemon.queued_ingest import QueuedIngest, QueuePolicy
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState, task_terminal
from tests.queue_fixtures import ctx_with_queue

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord

_COLLECTION = "proj-captures"
_QUICK = QueuePolicy(admit_timeout_s=0.2, finish_timeout_s=0.3, grace_s=0.1, reserve=0)


class _WorkerDied(BaseException):
    """Escapes the job and the worker's handlers: the worker task ends."""


@final
@dataclass(slots=True)
class _Unit:
    """A job that logs when it starts and ends, and can wait, block, fail or vanish."""

    name: str
    events: list[tuple[str, str]]
    collection: str = _COLLECTION
    delay: float = 0.0
    gate: asyncio.Event | None = None
    boom: bool = False
    dies: bool = False
    silent: bool = False
    kinds: list[str] = field(default_factory=list)
    spool: SpoolRecord | None = field(default=None)

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Log the start, wait as configured, log the end, record the outcome."""
        del ctx
        self.kinds.append(state.kind)
        if self.dies:
            raise _WorkerDied
        if self.silent:
            return  # no terminal state: the worker lives and the job never ends
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


def _hook_submit(ctx: DaemonContext, unit: _Unit) -> bool:
    """Admit *unit* the way a hook's capture route does (no reserve)."""
    state = TaskState(task_id=unit.name, kind="capture")
    return ctx.ingest_queue.try_submit(RouteKey("db", unit.collection), unit, state)


async def test_a_backfill_job_and_hook_jobs_for_one_collection_never_interleave(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    events: list[tuple[str, str]] = []
    assert _hook_submit(ctx, _Unit("hook-1", events, delay=0.05))
    backfill = asyncio.create_task(
        QueuedIngest(ctx, "backfill").run(_Unit("backfill", events, delay=0.05))
    )
    await asyncio.sleep(0.01)  # the backfill job is admitted behind hook-1
    assert _hook_submit(ctx, _Unit("hook-2", events, delay=0.05))

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


async def test_the_job_runs_under_the_kind_it_was_submitted_as(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    unit = _Unit("backfill", [])

    await QueuedIngest(ctx, "backfill").run(unit)
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert unit.kinds == ["backfill"]


async def test_a_full_queue_that_never_drains_refuses_the_job(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path, depth=1)
    events: list[tuple[str, str]] = []
    gate = asyncio.Event()
    assert _hook_submit(ctx, _Unit("hook", events, gate=gate))

    with pytest.raises(IngestRefusedError, match="not admitted in time") as raised:
        await QueuedIngest(ctx, "backfill", _QUICK).run(_Unit("backfill", events))

    gate.set()
    await ctx.ingest_queue.aclose(drain_timeout=2.0)
    assert ("start", "backfill") not in events
    assert raised.value.reason is IngestRefusal.NOT_ADMITTED


async def test_a_full_queue_that_drains_in_time_admits_the_job(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path, depth=1)
    events: list[tuple[str, str]] = []
    assert _hook_submit(ctx, _Unit("hook", events, delay=0.15))
    policy = QueuePolicy(admit_timeout_s=5.0, reserve=0)

    state = await QueuedIngest(ctx, "backfill", policy).run(_Unit("backfill", events))
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert state.status == "completed"
    assert events[-2:] == [("start", "backfill"), ("end", "backfill")]


async def test_a_job_that_fails_is_returned_failed_with_its_reason(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)

    state = await QueuedIngest(ctx, "backfill").run(_Unit("backfill", [], boom=True))
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert state.status == "failed"
    assert state.error == "internal error: RuntimeError"


async def test_queued_jobs_leave_no_entry_in_the_task_registry(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    queued = QueuedIngest(ctx, "backfill")

    for i in range(3):
        await queued.run(_Unit(f"backfill-{i}", []))
    await ctx.ingest_queue.aclose(drain_timeout=2.0)

    assert len(ctx.tasks) == 0


async def test_a_worker_that_dies_mid_job_is_reported_not_waited_on(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)
    policy = QueuePolicy(finish_timeout_s=30.0, reserve=0)
    started = time.monotonic()

    with pytest.raises(IngestRefusedError, match="worker stopped") as raised:
        await QueuedIngest(ctx, "backfill", policy).run(
            _Unit("backfill", [], dies=True)
        )

    assert time.monotonic() - started < 5.0  # not the 30s deadline
    assert raised.value.reason is IngestRefusal.WORKER_STOPPED


async def test_a_job_that_never_reaches_a_terminal_state_times_out(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path)

    with pytest.raises(
        IngestRefusedError, match=r"did not finish within 0\.3s"
    ) as raised:
        await QueuedIngest(ctx, "backfill", _QUICK).run(
            _Unit("backfill", [], silent=True)
        )
    await ctx.ingest_queue.aclose(drain_timeout=1.0)
    assert raised.value.reason is IngestRefusal.UNFINISHED


async def test_a_closed_queue_refuses_at_once_as_shutting_down(tmp_path: Path) -> None:
    ctx = ctx_with_queue(tmp_path)
    await ctx.ingest_queue.aclose(drain_timeout=1.0)
    policy = QueuePolicy(admit_timeout_s=30.0)
    started = time.monotonic()

    with pytest.raises(IngestRefusedError, match="daemon shutting down") as raised:
        await QueuedIngest(ctx, "backfill", policy).run(_Unit("backfill", []))

    assert time.monotonic() - started < 2.0
    assert raised.value.reason is IngestRefusal.SHUTTING_DOWN


async def test_a_wait_for_room_ends_when_the_daemon_starts_shutting_down(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path, depth=1)
    gate = asyncio.Event()
    assert _hook_submit(ctx, _Unit("hook", [], gate=gate))
    policy = QueuePolicy(admit_timeout_s=30.0, reserve=0)
    waiting = asyncio.create_task(
        QueuedIngest(ctx, "backfill", policy).run(_Unit("backfill", []))
    )
    await asyncio.sleep(0.1)  # the backfill job is waiting for room
    started = time.monotonic()

    closing = asyncio.create_task(ctx.ingest_queue.aclose(drain_timeout=2.0))
    await asyncio.sleep(0.05)
    gate.set()
    with pytest.raises(IngestRefusedError) as raised:
        await waiting
    await closing

    assert time.monotonic() - started < 5.0
    assert raised.value.reason is IngestRefusal.SHUTTING_DOWN


async def test_hooks_are_admitted_while_a_backfill_waits_for_headroom(
    tmp_path: Path,
) -> None:
    ctx = ctx_with_queue(tmp_path, depth=4)
    events: list[tuple[str, str]] = []
    gate = asyncio.Event()
    assert _hook_submit(ctx, _Unit("hook-1", events, gate=gate))
    assert _hook_submit(ctx, _Unit("hook-2", events, gate=gate))
    policy = QueuePolicy(admit_timeout_s=5.0, reserve=2)  # the backfill stops at 2
    backfill = asyncio.create_task(
        QueuedIngest(ctx, "backfill", policy).run(_Unit("backfill", events))
    )
    await asyncio.sleep(0.1)

    assert _hook_submit(ctx, _Unit("hook-3", events))  # a hook still gets in
    assert ("start", "backfill") not in events  # while the backfill waits

    gate.set()
    state = await backfill
    await ctx.ingest_queue.aclose(drain_timeout=2.0)
    assert state.status == "completed"
