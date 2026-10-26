"""Backfill's writer: each transcript is a job on its collection's FIFO."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.backfill_job import BackfillTranscriptJob
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.daemon.queued_ingest import DEFAULT_QUEUE_POLICY, QueuedIngest, QueuePolicy
from quarry.daemon.queued_job import QueuedJob

if TYPE_CHECKING:
    from datetime import datetime

    from quarry.backfill_session import BackfillSession
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.thread_call import ThreadCall


@final
class QueuedTranscriptWriter:
    """Write each backfilled transcript through the daemon's collection FIFO.

    Backfill's loop runs on a worker thread; each write hops to the event loop,
    waits for the job to finish, and returns only once the store says whether
    the transcript landed.  It is the same worker, in submission order, that a
    hook's capture of the same session goes through, so the two never interleave
    a delete with an insert.  The wait on the loop is bounded by the policy, and
    so is this thread's wait for the loop (:class:`ThreadCall`): a stopped or
    closed loop fails the run rather than blocking the thread for good.
    """

    __slots__ = ("_call", "_ctx", "_policy")

    _call: ThreadCall
    _ctx: DaemonContext
    _policy: QueuePolicy

    def __new__(
        cls,
        ctx: DaemonContext,
        call: ThreadCall,
        policy: QueuePolicy = DEFAULT_QUEUE_POLICY,
    ) -> Self:
        self = super().__new__(cls)
        self._call = call
        self._ctx = ctx
        self._policy = policy
        return self

    @staticmethod
    def job_for(
        collection: str,
        session: BackfillSession,
        snapshot: datetime,
        version: str = "",
    ) -> BackfillTranscriptJob:
        """Return the job that stores *session* over any earlier copy of it."""
        return BackfillTranscriptJob(
            ScrubbedIngestJob(
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
                source_version=version,
            )
        )

    def write(
        self,
        collection: str,
        session: BackfillSession,
        snapshot: datetime,
        version: str = "",
    ) -> None:
        """Store *session* in *collection*; raise if the worker did not store it."""
        queued = QueuedIngest(self._ctx, "backfill", self._policy)
        QueuedJob(self._call, queued).store(
            self.job_for(collection, session, snapshot, version)
        )
