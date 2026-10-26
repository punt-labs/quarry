"""ThreadStep: a step on a serial pool remembers whether it has really begun."""

from __future__ import annotations

import asyncio
import threading

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.thread_step import ThreadStep


async def test_a_step_is_queued_behind_its_predecessor_until_it_begins() -> None:
    pool = DaemonThreads(max_threads=1)
    release = threading.Event()

    def hold() -> None:
        release.wait(5)

    first = ThreadStep(pool, hold)
    second = ThreadStep(pool, lambda: None, behind="first")

    running = [asyncio.ensure_future(first.run()), asyncio.ensure_future(second.run())]
    await asyncio.sleep(0.1)

    assert first.queued_behind() is None
    assert second.queued_behind() == "first"  # the thread is still in the first

    release.set()
    await asyncio.gather(*running)

    assert second.queued_behind() is None
