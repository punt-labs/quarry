"""Backfill's writer: each transcript is a job on its collection's FIFO."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_writer import BackfillWriteError
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.daemon.queued_ingest import QueuedIngest

if TYPE_CHECKING:
    from datetime import datetime

    from quarry.backfill_session import BackfillSession
    from quarry.daemon.context import DaemonContext


@final
class QueuedTranscriptWriter:
    """Write each backfilled transcript through the daemon's collection FIFO.

    Backfill's loop runs on a worker thread; each write hops to the event loop,
    waits for the job to finish, and returns only once the store says whether
    the transcript landed.  It is the same worker, in submission order, that a
    hook's capture of the same session goes through, so the two never interleave
    a delete with an insert.
    """

    __slots__ = ("_loop", "_queued")

    _loop: asyncio.AbstractEventLoop
    _queued: QueuedIngest

    def __new__(
        cls,
        ctx: DaemonContext,
        loop: asyncio.AbstractEventLoop,
        admit_timeout_s: float = 60.0,
    ) -> Self:
        self = super().__new__(cls)
        self._loop = loop
        self._queued = QueuedIngest(ctx, admit_timeout_s)
        return self

    @staticmethod
    def job_for(
        collection: str, session: BackfillSession, snapshot: datetime
    ) -> ScrubbedIngestJob:
        """Return the job that stores *session* over any earlier copy of it."""
        return ScrubbedIngestJob(
            name=session.name,
            content=session.body,
            labels=IngestLabels(
                overwrite=True,
                collection=collection,
                agent_handle="",
                memory_type="",
                summary="",
            ),
            format_hint="plain",
            scrub_label="backfill",
            captured_at=snapshot,
        )

    def write(
        self, collection: str, session: BackfillSession, snapshot: datetime
    ) -> None:
        """Store *session* in *collection*; raise if the worker did not store it."""
        job = self.job_for(collection, session, snapshot)
        state = asyncio.run_coroutine_threadsafe(
            self._queued.run(job), self._loop
        ).result()
        if state.status != "completed":
            raise BackfillWriteError(state.error)
