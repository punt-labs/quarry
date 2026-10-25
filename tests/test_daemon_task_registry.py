"""Ref-lifecycle invariants for the daemon's background-task registry.

The daemon runs for days, so a strong ref that never leaves ``_refs`` is a slow
memory leak. These tests pin the invariant that ``track`` retains a task while
it runs and drops the ref once the task reaches a terminal state — including
the boundary case of a task that is *already done* when ``track`` is called.
"""

from __future__ import annotations

import asyncio

import pytest

from quarry.daemon.job_claim import JobClaim
from quarry.daemon.tasks import TaskRegistry, TaskState


async def _completed_task() -> asyncio.Task[None]:
    """Return a task that has already run to completion."""

    async def _body() -> None:
        return None

    task: asyncio.Task[None] = asyncio.ensure_future(_body())
    await task
    return task


class TestTrackRefLifecycle:
    """``track`` holds the task ref, then drops it at terminal state."""

    async def test_already_done_task_ref_dropped_after_one_tick(self) -> None:
        """A task done *before* track is tracked, then dropped on the next tick.

        ``add_done_callback`` on a finished task schedules the drop via
        ``loop.call_soon`` rather than running it inline, so the insert wins and
        the ref is present immediately after ``track``; one loop tick then runs
        the callback and clears it. Accessing ``_refs`` is deliberate white-box
        inspection of the exact structure whose leak this guards.
        """
        registry = TaskRegistry()
        state = registry.begin("sync")
        task = await _completed_task()

        registry.track(state, task)
        assert state.task_id in registry._refs  # inserted; drop not yet run

        await asyncio.sleep(0)  # run the scheduled done-callback
        assert state.task_id not in registry._refs  # ref released, no leak

    async def test_running_task_ref_dropped_when_it_completes(self) -> None:
        """A still-running task keeps its ref until it finishes, then drops it."""
        registry = TaskRegistry()
        state = registry.begin("ingest")
        gate = asyncio.Event()

        async def _body() -> None:
            await gate.wait()

        task: asyncio.Task[None] = asyncio.ensure_future(_body())
        registry.track(state, task)
        assert state.task_id in registry._refs  # held while running

        gate.set()
        await task  # let the body finish
        await asyncio.sleep(0)  # run the scheduled done-callback
        assert state.task_id not in registry._refs  # released on completion


class TestDrain:
    """``drain`` awaits real completion; a straggler fails closed, not silently."""

    async def test_completed_tasks_drain_without_error(self) -> None:
        """Tasks that finish inside the window: drain returns normally."""
        registry = TaskRegistry()
        state = registry.begin("sync")
        task = await _completed_task()
        registry.track(state, task)

        await registry.drain(timeout=1.0)  # no raise

    async def test_straggler_fails_closed_not_silently(self) -> None:
        """A task that outlives the window: drain cancels the wrapper and RAISES.

        Swallowing the timeout and returning normally would let a caller believe
        the underlying ``run_in_threadpool`` thread is gone when only the
        ``asyncio.Task`` wrapper was cancelled — the exact race this method
        exists to close. ``TimeoutError`` must propagate.
        """
        registry = TaskRegistry()
        state = registry.begin("ingest")
        never_finishes = asyncio.Event()  # never .set() — the straggler

        async def _body() -> None:
            await never_finishes.wait()

        task: asyncio.Task[None] = asyncio.ensure_future(_body())
        registry.track(state, task)

        with pytest.raises(TimeoutError):
            await registry.drain(timeout=0.01)

        assert task.cancelled()  # wrapper cancelled as best-effort cleanup


def test_a_task_whose_thread_is_alive_still_counts_as_running() -> None:
    registry = TaskRegistry()
    state = registry.begin("backfill")
    state.status = "failed"  # reported, but its scan thread has not returned
    state.holds_thread = True

    assert registry.running_of_kind("backfill") is state

    state.holds_thread = False
    assert registry.running_of_kind("backfill") is None


def test_a_queued_job_can_be_withdrawn_but_not_a_running_one() -> None:
    queued = TaskState(task_id="q", kind="backfill", status="queued")
    running = TaskState(task_id="r", kind="backfill", status="queued")
    assert JobClaim(running).start()

    assert JobClaim(queued).withdraw()
    assert JobClaim(queued).withdraw()  # again: it still never runs
    assert not JobClaim(queued).start()  # the worker skips it
    assert not JobClaim(running).withdraw()  # it is running; counted, not cancelled


def test_a_job_that_was_never_submitted_is_withdrawn() -> None:
    assert JobClaim(TaskState(task_id="n", kind="backfill", status="new")).withdraw()
