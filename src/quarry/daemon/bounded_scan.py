"""Run a blocking scan on a thread; cancelling the caller waits for the thread."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.late_outcome import LateOutcome
from quarry.daemon.scan_wait import ScanWait

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.scan_report import ScanReport
    from quarry.daemon.tasks import TaskState
    from quarry.stop_signal import StopSignal


@final
class BoundedScan:
    """A scan whose thread outlives a cancelled caller only until it has stopped.

    Cancelling an ``await`` on a thread does not stop the thread.  A task that
    reported itself finished at that point would let the singleton guard admit a
    second run beside the first, so a cancelled scan is told to stop (with its
    reason) and then awaited through every further cancel
    (:class:`~quarry.daemon.scan_wait.ScanWait`), and the cancellation
    propagates only once the thread has returned.  The scan's outcome is written
    to the task first, so the reason the run really stopped for survives.

    The wait is bounded.  When it gives up, the task is told what the scan has
    counted so far and why it stopped, and the task state keeps ``holds_thread``
    until the thread really returns, so the singleton guard still refuses a
    second run beside it; the thread's late outcome is written then.
    """

    __slots__ = ("_bound_s", "_stop")

    _bound_s: float
    _stop: StopSignal

    def __new__(cls, stop: StopSignal, bound_s: float) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._bound_s = bound_s
        return self

    async def run[T](
        self, scan: Callable[[], T], state: TaskState, report: ScanReport[T]
    ) -> T:
        """Return *scan*'s result, recording it; stop and outwait it if cancelled."""
        state.holds_thread = True
        running = asyncio.ensure_future(run_in_threadpool(scan))
        late = LateOutcome(state, report)
        running.add_done_callback(late.returned)
        cancelled = await self._waiter().until_done(running)
        return late.settle(running, cancelled=cancelled, reason=self._reason())

    def _waiter(self) -> ScanWait:
        """Return the wait that absorbs cancels and gives up at the bound."""
        return ScanWait(self._stop, self._bound_s)

    def _reason(self) -> str:
        """Return why the scan was stopped, or ``""`` if it was not."""
        return self._stop.reason
