"""Decide, under one lock, whether a queued job runs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class JobClaim:
    """The worker's moves on a job's state: queue it, then start it once.

    The producer waiting for a job and the worker that will run it are on
    different threads.  Each move takes the state's one lock, so a job a producer
    withdrew while it waited is never started
    (:class:`~quarry.daemon.job_withdrawal.JobWithdrawal`).
    """

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def enqueue(self) -> bool:
        """Mark the job ``queued``; return False if it was already withdrawn."""
        with self._state.claim:
            if self._state.status == "withdrawn":
                return False
            self._state.status = "queued"
            return True

    def is_queued(self) -> bool:
        """Return whether the job is still waiting its turn."""
        return self._state.status == "queued"

    def start(self) -> bool:
        """Move a queued job to ``running``; return False if it will not run."""
        with self._state.claim:
            if self._state.status == "queued":
                self._state.status = "running"
                return True
            self._skipped()
            return False

    def _skipped(self) -> None:
        """Log a job that was not started for any reason but being withdrawn."""
        if self._state.status != "withdrawn":
            logger.warning("job not started: its status is %s", self._state.status)
