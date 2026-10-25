"""Run a blocking scan on a thread; cancelling the caller waits for the thread."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, ClassVar, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.late_outcome import LateOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.backfill_stats import BackfillStats
    from quarry.daemon.run_record import RunRecord
    from quarry.daemon.tasks import TaskState
    from quarry.stop_signal import StopSignal


@final
class BoundedScan:
    """A scan whose thread outlives a cancelled caller only until it has stopped.

    Cancelling an ``await`` on a thread does not stop the thread.  A task that
    reported itself finished at that point would let the singleton guard admit a
    second run beside the first, so a cancelled scan is told to stop (with its
    reason) and then awaited through every further cancel, and the cancellation
    propagates only once the thread has returned.  The scan's outcome is written
    to the task first, so the reason the run really stopped for survives.

    The wait is bounded.  A cancel sets the stop (with its reason) and the wait
    goes on: the thread ends at its next step.  A step the stop cannot interrupt
    (a read, a listing) can hold the thread, so :attr:`bound_s` after the stop
    was requested, by a cancel or by anything else, the wait gives up.  The task
    is then told what the scan has counted so far and why it stopped, and the
    task state keeps ``holds_thread`` until the thread really returns, so the
    singleton guard still refuses a second run beside it; the thread's late
    outcome is written then.
    """

    # How often the wait looks at the clock, and at whether a stop was requested.
    POLL_S: ClassVar[float] = 0.1

    __slots__ = ("_bound_s", "_stop")

    _bound_s: float
    _stop: StopSignal

    def __new__(cls, stop: StopSignal, bound_s: float) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._bound_s = bound_s
        return self

    async def run(
        self, scan: Callable[[], BackfillStats], state: TaskState, record: RunRecord
    ) -> BackfillStats:
        """Return *scan*'s result, recording it; stop and outwait it if cancelled."""
        state.holds_thread = True
        running = asyncio.ensure_future(run_in_threadpool(scan))
        late = LateOutcome(state, record)
        running.add_done_callback(late.returned)
        cancelled = await self._until_done(running)
        return late.settle(running, cancelled=cancelled, reason=self._stop.reason)

    async def _until_done(self, running: asyncio.Future[BackfillStats]) -> bool:
        """Wait for *running* or the bound; return whether a cancel arrived."""
        cancelled = False
        stopped_at = 0.0
        while not running.done():
            try:
                await asyncio.wait({running}, timeout=self.POLL_S)
            except asyncio.CancelledError:
                self._stop.set("task cancelled")
                cancelled = True
            if self._stop.is_set():
                stopped_at = stopped_at or time.monotonic()
                if time.monotonic() - stopped_at >= self._bound_s:
                    break
        return cancelled
