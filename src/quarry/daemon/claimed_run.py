"""Run a queued job for its worker, unless its producer has withdrawn it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.job_claim import JobClaim

if TYPE_CHECKING:
    import asyncio

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState


@final
class ClaimedRun:
    """The worker's side of a job's claim: queue it, then run it if still wanted.

    The claim is checked before the embed gate, so a withdrawn job never waits
    for it, and again once the gate is held, so a job withdrawn while it waited
    for the gate is skipped too.  The job records its own terminal state.
    """

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def enqueue(self) -> bool:
        """Mark the job queued; a job already withdrawn stays withdrawn."""
        return JobClaim(self._state).enqueue()

    async def run(
        self, job: IngestUnit, ctx: DaemonContext, embed_gate: asyncio.Semaphore
    ) -> None:
        """Run *job* under *embed_gate* unless it was withdrawn."""
        claim = JobClaim(self._state)
        if claim.is_queued():
            async with embed_gate:  # global embed-concurrency bound
                if claim.start():
                    await job.run(ctx, self._state)
