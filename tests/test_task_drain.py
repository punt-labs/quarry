"""TaskDrain: await tasks for real, and give a task that will not cancel up."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from quarry.daemon.task_drain import TaskDrain


async def _finishes() -> None:
    await asyncio.sleep(0.01)


async def _ignores_cancel(release: asyncio.Event) -> None:
    while not release.is_set():
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            continue  # a body that swallows its cancellation


async def test_tasks_that_finish_inside_the_window_drain_quietly() -> None:
    tasks = [asyncio.ensure_future(_finishes()) for _ in range(3)]

    await TaskDrain(tasks, cancel_wait_s=1.0).run(timeout=1.0)

    assert all(task.done() for task in tasks)


async def test_a_straggler_is_cancelled_and_the_drain_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = asyncio.Event()

    async def waits() -> None:
        await gate.wait()

    task = asyncio.ensure_future(waits())

    with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
        await TaskDrain([task], cancel_wait_s=1.0).run(timeout=0.05)

    assert task.cancelled()
    assert "did not complete" in caplog.text


async def test_a_task_that_will_not_cancel_cannot_hold_shutdown_past_the_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()
    task = asyncio.ensure_future(_ignores_cancel(release))
    started = time.monotonic()

    with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
        await TaskDrain([task], cancel_wait_s=0.3).run(timeout=0.05)

    assert time.monotonic() - started < 2.0  # the drain window plus the bound
    assert not task.done()
    assert "did not stop" in caplog.text
    release.set()
    await task


async def test_the_cancel_wait_sits_inside_the_budget_not_after_it() -> None:
    release = asyncio.Event()
    task = asyncio.ensure_future(_ignores_cancel(release))
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await TaskDrain([task], cancel_wait_s=30.0).run(timeout=0.4)

    assert time.monotonic() - started < 0.8  # 0.4 in all, not 0.4 plus a 30 s cancel
    release.set()
    await task


async def test_a_budget_already_spent_cancels_at_once() -> None:
    gate = asyncio.Event()

    async def waits() -> None:
        await gate.wait()

    task = asyncio.ensure_future(waits())
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await TaskDrain([task], cancel_wait_s=30.0).run(timeout=0.0)

    assert time.monotonic() - started < 0.5
    assert task.cancelled()
