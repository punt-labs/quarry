"""ClaimedRun: the worker runs a job unless it was withdrawn, before or at the gate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from quarry.daemon.claimed_run import ClaimedRun
from quarry.daemon.job_claim import JobClaim
from quarry.daemon.job_withdrawal import JobWithdrawal
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit

_CTX = cast("DaemonContext", SimpleNamespace())


class _Job:
    def __init__(self) -> None:
        self.ran = 0

    async def run(self, ctx: object, state: TaskState) -> None:
        del ctx
        self.ran += 1
        state.status = "completed"


def _queued() -> TaskState:
    state = TaskState(task_id="j", kind="backfill", status="new")
    assert ClaimedRun(state).enqueue()
    return state


async def test_a_queued_job_runs_under_the_gate() -> None:
    state, job = _queued(), _Job()

    await ClaimedRun(state).run(cast("IngestUnit", job), _CTX, asyncio.Semaphore(1))

    assert (job.ran, state.status) == (1, "completed")


async def test_a_job_withdrawn_before_the_gate_is_skipped_without_taking_it() -> None:
    state, job = _queued(), _Job()
    JobWithdrawal(state).withdraw()
    gate = asyncio.Semaphore(0)  # a held gate: taking it would hang

    await asyncio.wait_for(
        ClaimedRun(state).run(cast("IngestUnit", job), _CTX, gate), timeout=2
    )

    assert job.ran == 0


async def test_a_job_withdrawn_while_it_waits_for_the_gate_is_skipped_at_the_gate() -> (
    None
):
    state, job = _queued(), _Job()
    gate = asyncio.Semaphore(0)
    running = asyncio.create_task(
        ClaimedRun(state).run(cast("IngestUnit", job), _CTX, gate)
    )
    await asyncio.sleep(0.05)  # it is queued, waiting on the gate

    JobWithdrawal(state).withdraw()
    gate.release()
    await asyncio.wait_for(running, timeout=2)

    assert job.ran == 0
    assert state.status == "withdrawn"


async def test_a_job_that_is_not_queued_is_not_run_twice() -> None:
    state, job = _queued(), _Job()
    assert JobClaim(state).start()  # already dispatched once

    await ClaimedRun(state).run(cast("IngestUnit", job), _CTX, asyncio.Semaphore(1))

    assert job.ran == 0
