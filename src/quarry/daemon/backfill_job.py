"""A backfilled transcript as an ingest unit: durable at its source, never spooled."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, final

if TYPE_CHECKING:
    from quarry.daemon.content_jobs import ScrubbedIngestJob
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.tasks import TaskState


@final
@dataclass(frozen=True, slots=True)
class BackfillTranscriptJob:
    """The scrubbed ingest of one transcript, which stays on disk to be read again.

    The transcript is the durable copy and backfill re-reads it, so a job the
    shutdown drain aborts is never spooled (a spooled copy of a whole
    conversation would be a second, unreviewed store of it): it fails with a
    reason that says to run backfill again.
    """

    inner: ScrubbedIngestJob

    rerun_hint: ClassVar[str] = "re-run backfill"

    @property
    def collection(self) -> str:
        """Return the collection this job writes (the queue routing key)."""
        return self.inner.collection

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Ingest the transcript, recording the outcome on *state*."""
        await self.inner.run(ctx, state)

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: the transcript outlives an abort; backfill re-reads it."""
        return None
