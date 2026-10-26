"""A queued job's status moves, each under the job's one claim."""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState, TaskStatus

logger = logging.getLogger(__name__)


@final
class JobLifecycle:
    """The only writer of a queued job's ``status``, ``error`` and ``results``.

    The producer waiting for a job, the worker that runs it, the store thread's
    checkpoint, the shutdown abort and the observer of a late store are on
    different threads.  Each move takes the state's one claim and checks what it
    is about to overwrite, so a job is started or withdrawn, committed or
    withdrawn, never both; a status somebody else settled is never replaced; and
    a job past its commit is *not* certain to store (the store deletes the
    earlier copy and then inserts, and the insert can still fail), so the
    producer that gives up on it is told it is :attr:`Withdrawn.COMMITTING`,
    never that it stored.

    The terminals, the jobs and the abort ask for a move; none assigns a status.
    """

    class Withdrawn(Enum):
        """Where a job stood when its producer withdrew it.

        ``NEVER_STARTED``: it never ran and now never will.  ``STARTED``: it was
        running but had not committed; it will stop at its commit and write
        nothing.  ``COMMITTING``: it is past its commit; it may store, or fail.
        ``STORED`` and ``FAILED``: it had already finished, so the answer is its
        own.
        """

        NEVER_STARTED = auto()
        STARTED = auto()
        COMMITTING = auto()
        STORED = auto()
        FAILED = auto()

    class Disposition(Enum):
        """What a shutdown abort found when it took a job.

        ``FAILED``: the job is now ``failed`` with the abort's reason.  ``KEPT``:
        its producer withdrew it or it stored; it stays what it is.
        ``UNCONFIRMED``: it is past its commit and its store has not ended; it
        stays ``committing``.
        """

        FAILED = auto()
        KEPT = auto()
        UNCONFIRMED = auto()

    # A job in one of these is left as it is when a shutdown aborts the queue.
    _KEPT_AT_ABORT: ClassVar[frozenset[TaskStatus]] = frozenset(
        {"withdrawn", "aborted", "completed"}
    )
    # A job in one of these has not started: it may still be withdrawn.
    _WAITING: ClassVar[frozenset[TaskStatus]] = frozenset({"new", "queued"})
    # A job in one of these is the worker's to settle with its terminal outcome.
    _WORKING: ClassVar[frozenset[TaskStatus]] = frozenset({"running", "committing"})

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
            self._state.is_job = True
            self._state.status = "queued"
            return True

    def start(self) -> bool:
        """Move a queued job to ``running``; return False if it will not run."""
        with self._state.claim:
            if self._state.status == "queued":
                self._state.status = "running"
                return True
            self._skipped()
            return False

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
            status = self._state.status
            if status in self._WAITING:
                self._state.status = status = "withdrawn"
            elif status == "running":
                self._state.status = status = "aborted"
            # A status this does not know is taken as started: nothing is
            # claimed untouched that might not be.
            return {
                "withdrawn": self.Withdrawn.NEVER_STARTED,
                "committing": self.Withdrawn.COMMITTING,
                "completed": self.Withdrawn.STORED,
                "failed": self.Withdrawn.FAILED,
            }.get(status, self.Withdrawn.STARTED)

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        """Complete the task with *results*; return whether it was.

        A job is completed only while the worker still owns it (running, or
        committing): the producer's withdrawal, and a status an earlier
        correction settled, are never overwritten.  The reason a shutdown abort
        gave it is cleared, so a completed job never carries a failure.
        """
        with self._state.claim:
            if not self._owned():
                return False
            self._state.status = "completed"
            self._state.error = ""
            self._state.results = dict(results)
            return True

    def settle_failed(self, reason: str) -> bool:
        """Fail the task with *reason*; return whether it was.

        Refused for a job the worker no longer owns: a producer's ``aborted``
        stays ``aborted``, and a job the abort already failed keeps its reason.
        """
        with self._state.claim:
            if not self._owned():
                return False
            self._state.status = "failed"
            self._state.error = reason
            return True

    def cancelled(self, default: str) -> None:
        """Record that the worker's body was cancelled.

        A job still ``running`` did not finish: it is ``failed`` with the reason
        it already gave, else *default*.  A committing job may still store, so it
        stays committing, and ``aborted`` and every settled status stay.  A task
        that never was a job is failed whatever it had recorded.
        """
        with self._state.claim:
            if self._state.is_job and self._state.status != "running":
                return
            self._state.status = "failed"
            self._state.error = self._state.reason_or(default)

    def exited(self, default: str) -> None:
        """Record that the worker's body exited with no terminal status of its own.

        A job still ``running`` is failed with the reason it already gave, else
        *default*; ``aborted`` and a committing job whose store runs are not
        "exited without a terminal status", the producer and the store own them.
        A task that never was a job is failed unless it is ``completed`` or
        ``failed``.
        """
        with self._state.claim:
            status = self._state.status
            finished = status in {"completed", "failed"}
            if (self._state.is_job and status != "running") or finished:
                return
            self._state.status = "failed"
            self._state.error = self._state.reason_or(default)

    def fail_aborted(self, reason: str) -> Disposition:
        """Fail the job for a shutdown abort, unless it is not the abort's to fail.

        The producer's withdrawal and a job that already stored stay what they
        truthfully are (:attr:`Disposition.KEPT`); one past its commit may still
        store and stays ``committing`` (:attr:`Disposition.UNCONFIRMED`).  Any
        other job is ``failed`` with *reason*, replacing an earlier one.
        """
        with self._state.claim:
            status = self._state.status
            if status in self._KEPT_AT_ABORT:
                return self.Disposition.KEPT
            if status == "committing":
                return self.Disposition.UNCONFIRMED
            self._state.status = "failed"
            self._state.error = reason
            return self.Disposition.FAILED

    def _owned(self) -> bool:
        """Return whether the worker may still settle this task (claim held)."""
        return not self._state.is_job or self._state.status in self._WORKING

    async def run(
        self, job: IngestUnit, ctx: DaemonContext, embed_gate: asyncio.Semaphore
    ) -> None:
        """Run *job* under *embed_gate* unless it was withdrawn.

        The claim is checked before the gate, so a withdrawn job never waits for
        it, and again once the gate is held, so a job withdrawn while it waited
        for the gate is skipped too.  The job records its own terminal state.
        """
        if self._state.status != "queued":
            self._skipped()
            return
        async with embed_gate:  # global embed-concurrency bound
            if self.start():
                await job.run(ctx, self._state)

    def _skipped(self) -> None:
        """Warn of a job not started for any reason but being withdrawn."""
        if self._state.status != "withdrawn":
            logger.warning("job not started: its status is %s", self._state.status)
