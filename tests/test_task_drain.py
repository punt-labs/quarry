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


async def test_tasks_that_finish_inside_the_budget_drain_quietly() -> None:
    tasks = [asyncio.ensure_future(_finishes()) for _ in range(3)]

    await TaskDrain(tasks).run(timeout=1.0)

    assert all(task.done() for task in tasks)


async def test_a_straggler_is_cancelled_and_the_drain_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = asyncio.Event()

    async def waits() -> None:
        await gate.wait()

    task = asyncio.ensure_future(waits())

    with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
        await TaskDrain([task]).run(timeout=0.2)

    assert task.cancelled()
    assert "did not complete" in caplog.text


async def test_a_task_that_will_not_cancel_cannot_hold_shutdown_past_the_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()
    task = asyncio.ensure_future(_ignores_cancel(release))
    budget = 0.4
    started = time.monotonic()

    with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
        await TaskDrain([task]).run(timeout=budget)

    assert time.monotonic() - started < budget * 1.5  # the wait and the cancel wait
    assert not task.done()
    assert "did not stop" in caplog.text
    release.set()
    await task


async def test_the_cancel_wait_sits_inside_the_budget_not_after_it() -> None:
    release = asyncio.Event()
    task = asyncio.ensure_future(_ignores_cancel(release))
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await TaskDrain([task]).run(timeout=0.4)

    # The budget is spent once, window and cancel wait together, not twice.
    assert time.monotonic() - started < 0.4 * 1.5
    release.set()
    await task


async def test_the_cancel_wait_is_a_share_of_the_budget() -> None:
    release = asyncio.Event()

    async def finishes_late() -> None:
        await asyncio.sleep(0.3)
        release.set()

    task = asyncio.ensure_future(finishes_late())

    # 0.3 s of work fits in what a 0.5 s budget leaves before the cancel share.
    await TaskDrain([task]).run(timeout=0.5)

    assert release.is_set()


async def test_a_budget_already_spent_cancels_at_once() -> None:
    gate = asyncio.Event()

    async def waits() -> None:
        await gate.wait()

    task = asyncio.ensure_future(waits())
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await TaskDrain([task]).run(timeout=0.0)

    assert time.monotonic() - started < 0.5
    assert task.cancelled()
