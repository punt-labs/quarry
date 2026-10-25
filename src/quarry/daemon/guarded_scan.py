"""Run a blocking scan on a thread; cancelling the caller waits for the thread."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.stop_signal import StopSignal


@final
class GuardedScan:
    """A scan whose thread outlives a cancelled caller only until it has stopped.

    Cancelling an ``await`` on a thread does not stop the thread.  A task that
    reported itself finished at that point would let the singleton guard admit a
    second run beside the first, so a cancelled scan is told to stop (with its
    reason) and then awaited, and the cancellation propagates only once the
    thread has returned.
    """

    __slots__ = ("_stop",)

    _stop: StopSignal

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        return self

    async def run[T](self, scan: Callable[[], T]) -> T:
        """Return *scan*'s result; on cancellation stop it, wait for it, re-raise."""
        running = asyncio.ensure_future(run_in_threadpool(scan))
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            self._stop.set("task cancelled")
            await asyncio.wait({running})  # the scan ends at its next transcript
            raise
