"""ThreadStep: a step on a serial pool remembers whether it has begun and ended."""

from __future__ import annotations

import asyncio
import threading

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.thread_step import ThreadStep


async def test_a_step_is_queued_behind_an_earlier_one_until_it_begins() -> None:
    pool = DaemonThreads(max_threads=1)
    release = threading.Event()

    def hold() -> None:
        release.wait(5)

    first = ThreadStep(pool, "first", hold)
    second = ThreadStep(pool, "second", lambda: None, (first,))

    running = [asyncio.ensure_future(first.run()), asyncio.ensure_future(second.run())]
    await asyncio.sleep(0.1)

    assert first.queued_behind() is None
    assert second.queued_behind() == "first"  # the thread is still in the first

    release.set()
    await asyncio.gather(*running)

    assert second.queued_behind() is None
    assert first.has_ended


async def test_a_step_names_the_first_earlier_step_that_has_not_ended() -> None:
    pool = DaemonThreads(max_threads=1)
    release = threading.Event()

    def hold() -> None:
        release.wait(5)

    stuck = ThreadStep(pool, "stuck", hold)
    idle = ThreadStep(pool, "idle", lambda: None, (stuck,))
    last = ThreadStep(pool, "last", lambda: None, (stuck, idle))

    running = [asyncio.ensure_future(s.run()) for s in (stuck, idle, last)]
    await asyncio.sleep(0.1)

    assert idle.queued_behind() == "stuck"
    assert last.queued_behind() == "stuck"  # not "idle": it is the stuck one
    release.set()
    await asyncio.gather(*running)
    assert last.queued_behind() is None


async def test_a_step_that_raised_has_ended() -> None:
    pool = DaemonThreads(max_threads=1)

    def boom() -> None:
        msg = "step failed"
        raise OSError(msg)

    step = ThreadStep(pool, "boom", boom)
    follower = ThreadStep(pool, "follower", lambda: None, (step,))

    await asyncio.gather(step.run(), return_exceptions=True)

    assert step.has_ended
    assert follower.queued_behind() is None
