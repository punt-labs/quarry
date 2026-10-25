"""What becomes of a scan thread that returns after its task gave up on it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    from quarry.backfill_stats import BackfillStats
    from quarry.daemon.run_record import RunRecord
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


class ScanStillRunningError(RefusalError):
    """The scan thread did not return within the bound after it was told to stop."""


@final
class LateOutcome:
    """The scan thread's last word: release the guard, then say what it found.

    The thread is done when its future is, whoever is still waiting: the task
    state stops holding it, which opens the singleton guard.  If the task had
    given up on the thread, its outcome arrives here and is written to the task
    state, whose error then says how the thread ended (it returned, or failed, by
    class), and a failure is logged by class and frames, never its message, so no
    asyncio "exception never retrieved" is left behind.
    """

    __slots__ = ("_given_up", "_pending", "_record", "_state", "_stopped")

    _given_up: bool
    _pending: str
    _record: RunRecord
    _state: TaskState
    _stopped: str

    def __new__(cls, state: TaskState, record: RunRecord) -> Self:
        self = super().__new__(cls)
        self._state = state
        self._record = record
        self._given_up = False
        self._stopped = ""
        self._pending = ""
        return self

    def settle(
        self,
        running: asyncio.Future[BackfillStats],
        *,
        cancelled: bool,
        reason: str,
    ) -> BackfillStats:
        """Return the scan's result; end a cancelled or abandoned scan as it must.

        A scan that returned has its outcome written to the task; one that did
        not is given up on.  A cancel is re-raised once that is done, with the
        task's error naming the reason the run really stopped for, never the
        bare cancel, and the class of a failure the cancel would otherwise hide.

        Raises:
            asyncio.CancelledError: The task was cancelled.
            ScanStillRunningError: The thread did not return within the bound.
        """
        if not running.done():
            self.give_up(reason)
            if cancelled:
                raise asyncio.CancelledError
            raise ScanStillRunningError(self._state.error)
        failure = running.exception()
        if failure is None:
            self._record.record(running.result())
        elif cancelled:
            self.log_failure(failure)  # the cancel hides it from the caller
        if cancelled:
            stopped = self._state.error or f"backfill stopped ({reason})"
            self._state.error = (
                stopped
                if failure is None
                else f"{stopped}; scan failed: {type(failure).__name__}"
            )
            raise asyncio.CancelledError
        return running.result()

    def give_up(self, reason: str) -> None:
        """Record what the scan has counted, and say the task still holds its thread."""
        self._given_up = True
        self._stopped = f"backfill stopped ({reason})"
        self._record.record(self._record.so_far())
        self._pending = f"{self._stopped}; scan thread still running"
        self._state.error = self._pending
        # At shutdown nothing starts another backfill, so the guard is not the news.
        refusal = (
            ""
            if reason == StopSignal.SHUTDOWN
            else "; a new backfill is refused until it returns"
        )
        logger.warning(
            "backfill stopped (%s) but its scan thread is still running%s",
            reason,
            refusal,
        )

    def returned(self, running: asyncio.Future[BackfillStats]) -> None:
        """Done callback: release the guard, and take a late outcome if one is due."""
        self._state.holds_thread = False
        if not self._given_up:
            return
        if running.cancelled():
            self._state.error = f"{self._stopped}; scan thread cancelled"
            return
        failure = running.exception()
        if failure is not None:
            self.log_failure(failure)
            how = f"failed late: {type(failure).__name__}"
        else:
            self._record.late(running.result())
            how = f"returned late: {running.result().summary()}"
        # Appended to what the run record set, if it set a reason; else to the stop.
        recorded = self._state.error
        base = self._stopped if recorded == self._pending else recorded
        self._state.error = f"{base}; scan thread {how}"

    @staticmethod
    def log_failure(exc: BaseException) -> None:
        """Log how the scan failed: its class and frames, never its message."""
        logger.error(
            "backfill scan failed: %s\n%s",
            type(exc).__name__,
            FailureReason.frames(exc),
        )
