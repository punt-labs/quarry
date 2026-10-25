"""Decide, under one lock, whether a queued job runs or is withdrawn."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState

# A job in one of these has not started: it may still be withdrawn.
_WAITING = frozenset({"new", "queued"})


@final
class JobClaim:
    """The three moves on a job's state that must not race: queue, run, withdraw.

    The producer waiting for a job and the worker that will run it are on
    different threads.  Each move takes the state's one lock, so a job is run or
    withdrawn, never both: a job a producer gave up on while it waited is never
    run, and a job already running is never reported as untouched.
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

    def start(self) -> bool:
        """Move a queued job to ``running``; return False if it was withdrawn."""
        with self._state.claim:
            if self._state.status != "queued":
                return False
            self._state.status = "running"
            return True

    async def run(
        self, job: IngestUnit, ctx: DaemonContext, embed_gate: asyncio.Semaphore
    ) -> None:
        """Run *job* under *embed_gate* unless its producer withdrew it first.

        The job records its own terminal state.  A withdrawn job is skipped before
        it takes the gate: nothing the producer gave up on is written later.
        """
        if self.start():  # queued -> running on dequeue
            async with embed_gate:  # global embed-concurrency bound
                await job.run(ctx, self._state)

    def withdraw(self) -> bool:
        """Return whether the job will never run, withdrawing it if it is waiting."""
        with self._state.claim:
            if self._state.status in _WAITING:
                self._state.status = "withdrawn"
            return self._state.status == "withdrawn"
