"""A queued job's status moves, each under the job's one lock."""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    import asyncio

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState, TaskStatus

logger = logging.getLogger(__name__)


@final
class JobLifecycle:
    """The moves on a job's status that must not race: queue, start, commit, withdraw.

    The producer waiting for a job, the worker that runs it and the job about to
    store are on different threads.  Each move takes the state's one lock, so a
    job is started or withdrawn, committed or withdrawn, never both: a job
    withdrawn while it waited is never started, one withdrawn after it started
    stops at its commit instead of writing after its run was reported, and one
    that got past its commit can no longer be withdrawn.  Past its commit a job
    is *not* certain to store (the store deletes the earlier copy and then
    inserts, and the insert can still fail), so the producer that gives up on it
    is told it is :attr:`Withdrawn.COMMITTING`, never that it stored.
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

    # A job in one of these has not started: it may still be withdrawn.
    _WAITING: ClassVar[frozenset[TaskStatus]] = frozenset({"new", "queued"})

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
