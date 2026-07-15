"""Ref-lifecycle invariants for the daemon's background-task registry.

The daemon runs for days, so a strong ref that never leaves ``_refs`` is a slow
memory leak. These tests pin the invariant that ``track`` retains a task while
it runs and drops the ref once the task reaches a terminal state — including
the boundary case of a task that is *already done* when ``track`` is called.
"""

from __future__ import annotations

import asyncio

from quarry.daemon.tasks import TaskRegistry


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
