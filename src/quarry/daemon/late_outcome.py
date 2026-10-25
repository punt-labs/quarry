"""What becomes of a scan thread that returns after its task gave up on it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.daemon.scan_report import ScanReport
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


class ScanStillRunningError(RefusalError):
    """The scan thread did not return within the bound after it was told to stop."""


@final
class LateOutcome[T]:
    """The scan thread's last word: release the guard, then say what it found.

    The thread is done when its future is, whoever is still waiting: the task
    state stops holding it, which opens the singleton guard.  If the task had
    given up on the thread, its outcome arrives here and is written to the task
    state, and a failure is logged by class and frames, never its message, so no
    asyncio "exception never retrieved" is left behind.
    """

    __slots__ = ("_given_up", "_report", "_state")

    _given_up: bool
    _report: ScanReport[T]
    _state: TaskState

    def __new__(cls, state: TaskState, report: ScanReport[T]) -> Self:
        self = super().__new__(cls)
        self._state = state
        self._report = report
        self._given_up = False
        return self

    def settle(self, running: asyncio.Future[T], *, cancelled: bool, reason: str) -> T:
        """Return the scan's result; end a cancelled or abandoned scan as it must.

        A scan that returned has its outcome written to the task; one that did
        not is given up on.  A cancel is re-raised once that is done, with the
        task's error naming the reason the run really stopped for, never the
        bare cancel.

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
            self._report.record(running.result())
        elif cancelled:
            self.log_failure(failure)  # the cancel hides it from the caller
        if cancelled:
            self._state.error = self._state.error or f"backfill stopped ({reason})"
            raise asyncio.CancelledError
        return running.result()

    def give_up(self, reason: str) -> None:
        """Record what the scan has counted, and say the task still holds its thread."""
        self._given_up = True
        self._report.record(self._report.so_far())
        self._state.error = f"backfill stopped ({reason}); scan thread still running"
        logger.warning(
            "backfill stopped (%s) but its scan thread is still running; "
            "a new backfill is refused until it returns",
            reason,
        )

    def returned(self, running: asyncio.Future[T]) -> None:
        """Done callback: release the guard, and take a late outcome if one is due."""
        self._state.holds_thread = False
        if not self._given_up or running.cancelled():
            return
        failure = running.exception()
        if failure is not None:
            self.log_failure(failure)
        else:
            self._report.late(running.result())

    @staticmethod
    def log_failure(exc: BaseException) -> None:
        """Log how the scan failed: its class and frames, never its message."""
        logger.error(
            "backfill scan failed: %s\n%s",
            type(exc).__name__,
            FailureReason.frames(exc),
        )
