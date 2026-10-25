"""The run lifecycle replays the traces its Z model (docs/design) found.

Each case is a trace of the model: the operations it takes (Cancel, GateClose,
Hang, FoeEnd ...) against the real task, queue, worker and writer, and the state
the model says the run must end in.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.backfill_session import BackfillSession
from quarry.backfill_stats import BackfillStats
from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.backfill_args import BackfillArgs
from quarry.daemon.backfill_task import BackfillTask
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.task_terminal import TaskTerminal
from tests.queue_fixtures import BlockingUnit, admit_like_a_hook, ctx_with_queue

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from quarry.backfill import BackfillRuntime
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState

_ARGS = BackfillArgs(dry_run=False, collection="", project="", limit=0)
_SETTLE = 0.3  # long enough for the poll loops (0.1s and 0.5s) to have looked


@dataclass(slots=True)
class _World:
    """The real queue and task, and the levers a trace pulls."""

    ctx: DaemonContext
    state: TaskState
    tmp_path: Path
    scan_release: threading.Event
    foe_gate: asyncio.Event
    wrote: list[str]
    task: asyncio.Task[None] | None = None

    def session(self) -> BackfillSession:
        transcript = self.tmp_path / "abcd1234-0000-0000-0000-000000000000.jsonl"
        transcript.write_text("{}\n")
        return BackfillSession(transcript, str(self.tmp_path), "the conversation")


def _scan_that_hangs_in_a_step(world: _World) -> Callable[..., BackfillStats]:
    """A scan blocked in a step the stop cannot interrupt, then reporting the stop."""

    def scan(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        world.scan_release.wait(timeout=10)
        reason = runtime.stop.reason
        return BackfillStats(
            not_examined=1,
            stopped=reason,
            errors=[f"backfill stopped: {reason}"] if reason else [],
        )

    return scan


def _scan_that_writes_one(world: _World) -> Callable[..., BackfillStats]:
    """A scan whose one transcript goes through the real writer and queue."""

    def scan(_config: object, runtime: BackfillRuntime) -> BackfillStats:
        try:
            runtime.writer.write("proj-captures", world.session(), datetime.now(UTC))
        except BackfillWriteError as exc:
            assert exc.outcome in {
                WriteOutcome.ABORTED,
                WriteOutcome.ABORTED_RUNNING,
            }
            return BackfillStats(
                not_attempted=int(exc.outcome is WriteOutcome.ABORTED),
                failed=int(exc.outcome is WriteOutcome.ABORTED_RUNNING),
                stopped=str(exc),
                errors=[f"backfill stopped: {exc}"],
            )
        return BackfillStats(ingested=1)

    return scan


async def _pull(world: _World, step: str) -> None:
    """Take one operation of the model."""
    match step:
        case "cancel":
            assert world.task is not None
            world.task.cancel()
        case "gate_close":
            asyncio.get_running_loop().create_task(
                world.ctx.ingest_queue.aclose(drain_timeout=5.0)
            )
        case "release_scan":
            world.scan_release.set()
        case "release_foe":
            world.foe_gate.set()
        case _:
            msg = f"unknown step {step}"
            raise AssertionError(msg)
    await asyncio.sleep(_SETTLE)


async def _replay(
    tmp_path: Path,
    scan_for: Callable[[_World], Callable[..., BackfillStats]],
    steps: tuple[str, ...],
    *,
    depth: int = 32,
    bound_s: float = 30.0,
    foe: bool = False,
) -> _World:
    ctx = ctx_with_queue(tmp_path, depth=depth)
    world = _World(
        ctx,
        ctx.tasks.begin("backfill"),
        tmp_path,
        threading.Event(),
        asyncio.Event(),
        [],
    )
    if foe:
        admit_like_a_hook(ctx, BlockingUnit(world.foe_gate))

    async def records(_self: ScrubbedIngestJob, _ctx: object, state: TaskState) -> None:
        world.wrote.append("written")
        state.status = "completed"

    async def as_the_route_runs_it() -> None:
        with TaskTerminal.recording(world.state):
            await BackfillTask(ctx, bound_s=bound_s).run(world.state, _ARGS)

    with (
        patch("quarry.backfill.Backfill.sessions", scan_for(world)),
        patch.object(ScrubbedIngestJob, "run", records),
    ):
        world.task = asyncio.create_task(as_the_route_runs_it())
        await asyncio.sleep(_SETTLE)
        for step in steps:
            await _pull(world, step)
        world.scan_release.set()
        world.foe_gate.set()
        await asyncio.gather(world.task, return_exceptions=True)
        await ctx.ingest_queue.aclose(drain_timeout=2.0)
    return world


async def test_a_double_cancel_still_waits_for_the_thread_and_keeps_the_reason(
    tmp_path: Path,
) -> None:
    # Model: Cancel, Cancel, ReturnStopped, Settle  (L1, L3)
    ctx = ctx_with_queue(tmp_path)
    state = ctx.tasks.begin("backfill")
    release = threading.Event()
    world = _World(ctx, state, tmp_path, release, asyncio.Event(), [])

    async def as_the_route_runs_it() -> None:
        with TaskTerminal.recording(state):
            await BackfillTask(ctx).run(state, _ARGS)

    with patch("quarry.backfill.Backfill.sessions", _scan_that_hangs_in_a_step(world)):
        world.task = asyncio.create_task(as_the_route_runs_it())
        await asyncio.sleep(_SETTLE)
        await _pull(world, "cancel")
        await _pull(world, "cancel")

        assert not world.task.done()  # the second cancel did not abandon the wait
        assert ctx.tasks.running_of_kind("backfill") is state  # the guard is shut
        await _pull(world, "release_scan")

    assert world.task.cancelled()
    assert state.results["stop_reason"] == "task cancelled"
    assert ctx.tasks.running_of_kind("backfill") is None


async def test_the_first_stop_reason_is_the_one_reported_after_a_cancel(
    tmp_path: Path,
) -> None:
    # Model: GateClose, Cancel, ReturnStopped, Settle  (L3)
    world = await _replay(
        tmp_path, _scan_that_hangs_in_a_step, ("gate_close", "cancel", "release_scan")
    )

    assert world.state.results["stop_reason"] == "daemon shutting down"


async def test_a_stop_during_admission_never_enqueues_the_job(tmp_path: Path) -> None:
    # Model: Examine, GateClose, AbortWait(jadmit), ReturnStopped, Settle  (L2, L4)
    world = await _replay(
        tmp_path,
        _scan_that_writes_one,
        ("gate_close",),
        depth=1,
        foe=True,  # the one slot is a hook's job: the transcript waits to be admitted
    )

    assert world.wrote == []
    assert world.state.results["not_attempted"] == 1
    assert str(world.state.results["stop_reason"]).startswith("daemon shutting down")


async def test_a_stop_behind_a_hung_worker_never_lets_the_job_write_later(
    tmp_path: Path,
) -> None:
    # Model: FoeStart, Examine, Admit, Cancel, AbortWait(jpending), FoeEnd  (L2)
    world = await _replay(
        tmp_path,
        _scan_that_writes_one,
        ("cancel", "release_foe"),
        foe=True,  # the worker is held by a hook's job that has hung
    )

    assert world.wrote == []  # the worker freed itself, and skipped the job
    assert world.state.results["not_attempted"] == 1


async def test_a_thread_hung_in_a_step_is_given_up_on_but_still_holds_the_guard(
    tmp_path: Path,
) -> None:
    # Model: Hang, Cancel, Bound, Start2 refused, Unhang, ReturnStopped, Start2  (L1)
    ctx = ctx_with_queue(tmp_path)
    state = ctx.tasks.begin("backfill")
    world = _World(ctx, state, tmp_path, threading.Event(), asyncio.Event(), [])

    async def as_the_route_runs_it() -> None:
        with TaskTerminal.recording(state):
            await BackfillTask(ctx, bound_s=0.3).run(state, _ARGS)

    with patch("quarry.backfill.Backfill.sessions", _scan_that_hangs_in_a_step(world)):
        world.task = asyncio.create_task(as_the_route_runs_it())
        await asyncio.sleep(_SETTLE)
        started = time.monotonic()
        await _pull(world, "cancel")
        await asyncio.wait({world.task}, timeout=5)

        assert world.task.done()  # the wait gave up after the bound
        assert time.monotonic() - started < 3.0
        assert state.status == "failed"  # reported ...
        assert ctx.tasks.running_of_kind("backfill") is state  # ... but not free
        await _pull(world, "release_scan")

    assert ctx.tasks.running_of_kind("backfill") is None  # now a second run may start


@pytest.mark.parametrize("cancels", [1, 2, 3])
async def test_any_number_of_cancels_ends_the_same_way(
    tmp_path: Path, cancels: int
) -> None:
    world = await _replay(
        tmp_path,
        _scan_that_hangs_in_a_step,
        ("cancel",) * cancels + ("release_scan",),
    )

    assert world.task is not None
    assert world.task.cancelled()
    assert world.state.results["stop_reason"] == "task cancelled"
    assert not world.state.holds_thread
