"""What a backfill run makes of a job whose wait ended without an answer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.job_withdrawal import JobWithdrawal
from quarry.daemon.withdrawn import Withdrawn

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskState


@final
class AbortedJob:
    """Settle a job the run stopped waiting for, by where it stood.

    A job that had not started never runs; one that had started is stopped at its
    commit and writes nothing (it is counted failed, since it did not finish);
    one past its commit will store, and one that already finished is counted as it
    finished.  Nothing the run gave up on is written after the run is reported,
    and nothing that was written is counted as lost.
    """

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def settle(self, cause: Exception) -> None:
        """Return if the job stored or finished by itself; else raise how it ended.

        Raises:
            BackfillWriteError: The job never started (``ABORTED``) or was stopped
                at its commit (``ABORTED_RUNNING``).
        """
        match JobWithdrawal(self._state).withdraw():
            case Withdrawn.NEVER_STARTED:
                outcome = WriteOutcome.ABORTED
            case Withdrawn.STARTED:
                outcome = WriteOutcome.ABORTED_RUNNING
            case _:
                return  # it stored, is storing, or failed with its own reason
        raise BackfillWriteError(str(cause)).as_outcome(outcome) from cause
