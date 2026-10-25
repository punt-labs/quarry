"""Wait for a scan thread through every cancel, but not past a bound."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.stop_signal import StopSignal

# How often the wait looks at the clock, and at whether a stop was requested.
_POLL_S = 0.1


@final
class ScanWait:
    """Await a scan thread, absorbing every cancel, for *bound_s* after a stop.

    A cancel sets the stop (with its reason) and the wait goes on: the thread
    ends at its next step.  A step the stop cannot interrupt (a read, a listing)
    can hold the thread, so *bound_s* after the stop was requested, by a cancel
    or by anything else, the wait gives up and says so.
    """

    __slots__ = ("_bound_s", "_stop")

    _bound_s: float
    _stop: StopSignal

    def __new__(cls, stop: StopSignal, bound_s: float) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._bound_s = bound_s
        return self

    async def until_done[T](self, running: asyncio.Future[T]) -> bool:
        """Wait for *running* or the bound; return whether a cancel arrived."""
        cancelled = False
        stopped_at = 0.0
        while not running.done():
            try:
                await asyncio.wait({running}, timeout=_POLL_S)
            except asyncio.CancelledError:
                self._stop.set("task cancelled")
                cancelled = True
            if self._stop.is_set():
                stopped_at = stopped_at or time.monotonic()
                if time.monotonic() - stopped_at >= self._bound_s:
                    break
        return cancelled
