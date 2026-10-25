"""Decide, under the job's lock, how far a job goes once it may be withdrawn."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.withdrawn import Withdrawn

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskState

# A job in one of these has not started: it may still be withdrawn.
_WAITING = frozenset({"new", "queued"})

# What each status says about a withdrawn job.  A status this does not know is
# taken as started: nothing is claimed untouched that might not be.
_VERDICTS = {
    "withdrawn": Withdrawn.NEVER_STARTED,
    "aborted": Withdrawn.STARTED,
    "committing": Withdrawn.COMMITTING,
    "completed": Withdrawn.STORED,
    "failed": Withdrawn.FAILED,
}


@final
class JobWithdrawal:
    """The producer's and the job's moves on a job's state that must not race.

    The producer that gave up waiting and the job that is about to store are on
    different threads.  Each move takes the state's one lock, so a job is
    committed or withdrawn, never both: a job withdrawn after it started stops at
    its commit instead of writing after its run was reported, and one that got
    past its commit is reported stored.
    """

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def commit(self) -> bool:
        """Move a running job past its commit; return False if it was withdrawn."""
        with self._state.claim:
            if self._state.status != "running":
                return False
            self._state.status = "committing"
            return True

    def withdraw(self) -> Withdrawn:
        """Withdraw the job unless it has committed; say where it stood."""
        with self._state.claim:
            if self._state.status in _WAITING:
                self._state.status = "withdrawn"
            elif self._state.status == "running":
                self._state.status = "aborted"
            return _VERDICTS.get(self._state.status, Withdrawn.STARTED)
