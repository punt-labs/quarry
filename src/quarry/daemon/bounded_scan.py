"""Run a blocking scan on a thread; cancelling the caller waits for the thread."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.tasks import TaskState
    from quarry.stop_signal import StopSignal

logger = logging.getLogger(__name__)

# How often the wait looks at the clock, and at whether a stop was requested.
_POLL_S = 0.1


class ScanStillRunningError(RefusalError):
    """The scan thread did not return within the bound after it was told to stop."""


@final
class BoundedScan:
    """A scan whose thread outlives a cancelled caller only until it has stopped.

    Cancelling an ``await`` on a thread does not stop the thread.  A task that
    reported itself finished at that point would let the singleton guard admit a
    second run beside the first, so a cancelled scan is told to stop (with its
    reason) and then awaited through every further cancel, and the cancellation
    propagates only once the thread has returned.  The scan's result is handed
    to *record* first, so the reason the run really stopped for survives the
    cancel.

    The wait is bounded.  The scan checks the stop signal before each step it
    controls, but a step blocked in the operating system (a read, a listing)
    cannot be interrupted, so *bound_s* after the stop was requested the wait
    gives up rather than hold the event loop's task for good.  The task state
    keeps ``holds_thread`` until the thread really returns, so the singleton
    guard still refuses a second run beside it.
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
        self, scan: Callable[[], T], state: TaskState, record: Callable[[T], None]
    ) -> T:
        """Return *scan*'s result, recording it; stop and outwait it if cancelled.

        Raises:
            asyncio.CancelledError: The task was cancelled, once the thread has
                returned (or been given up on).
            ScanStillRunningError: The scan was told to stop and did not return
                within the bound.
        """
        state.holds_thread = True
        running = asyncio.ensure_future(run_in_threadpool(scan))
        running.add_done_callback(lambda _done: self._returned(state))
        cancelled = await self._wait(running)
        if not running.done():
            if cancelled:
                raise asyncio.CancelledError
            msg = "scan thread still running after the stop; a new run is refused"
            raise ScanStillRunningError(msg)
        failure = running.exception()
        if failure is not None:
            self._log_failure(failure)
        else:
            record(running.result())
        if cancelled:
            raise asyncio.CancelledError
        return running.result()

    async def _wait[T](self, running: asyncio.Future[T]) -> bool:
        """Wait for *running*, absorbing cancels; return whether one arrived."""
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

    @staticmethod
    def _returned(state: TaskState) -> None:
        """Release the singleton guard's hold: the thread is gone."""
        state.holds_thread = False

    @staticmethod
    def _log_failure(exc: BaseException) -> None:
        """Log how the scan failed: its class and frames, never its message."""
        logger.error(
            "backfill scan failed: %s\n%s",
            type(exc).__name__,
            FailureReason.frames(exc),
        )
