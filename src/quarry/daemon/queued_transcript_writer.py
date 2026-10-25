"""Backfill's writer: each transcript is a job on its collection's FIFO."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.backfill_job import BackfillTranscriptJob
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError
from quarry.daemon.queued_ingest import DEFAULT_QUEUE_POLICY, QueuedIngest, QueuePolicy
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.thread_call import ThreadCall, ThreadCallAbortedError

# What each way the wait can end means for the backfill run.  An unfinished job
# aborts it as a dead worker does: a hung worker would otherwise collect one
# orphaned job, and one full wait, per transcript.
_OUTCOMES = {
    IngestRefusal.SHUTTING_DOWN: WriteOutcome.ABORTED,
    IngestRefusal.WORKER_STOPPED: WriteOutcome.ABORTED,
    IngestRefusal.NOT_ADMITTED: WriteOutcome.NOT_ADMITTED,
    IngestRefusal.UNFINISHED: WriteOutcome.ABORTED,
}

if TYPE_CHECKING:
    from datetime import datetime

    from quarry.backfill_session import BackfillSession
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


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

    __slots__ = ("_call", "_queued")

    _call: ThreadCall
    _queued: QueuedIngest

    def __new__(
        cls,
        ctx: DaemonContext,
        call: ThreadCall,
        policy: QueuePolicy = DEFAULT_QUEUE_POLICY,
    ) -> Self:
        self = super().__new__(cls)
        self._call = call
        self._queued = QueuedIngest(ctx, "backfill", policy)
        return self

    @staticmethod
    def job_for(
        collection: str, session: BackfillSession, snapshot: datetime
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
            )
        )

    def write(
        self, collection: str, session: BackfillSession, snapshot: datetime
    ) -> None:
        """Store *session* in *collection*; raise if the worker did not store it."""
        job = self.job_for(collection, session, snapshot)
        state = self._submit(job)
        if state.status == "completed":
            return
        error = BackfillWriteError(state.error)
        if state.error == RefusalAwareTerminal.NOTHING_STORED:
            error.as_outcome(WriteOutcome.UNINDEXABLE)
        raise error

    def _submit(self, job: BackfillTranscriptJob) -> TaskState:
        """Run *job* on the loop and return its terminal state, or raise the refusal."""
        try:
            return self._call.result(self._queued.run(job))
        except IngestRefusedError as exc:
            outcome = _OUTCOMES[exc.reason]
            raise BackfillWriteError(str(exc)).as_outcome(outcome) from exc
        except ThreadCallAbortedError as exc:
            raise self._aborted(str(exc)) from exc

    @staticmethod
    def _aborted(reason: str) -> BackfillWriteError:
        """Return the error that stops the run for *reason*."""
        return BackfillWriteError(reason).as_outcome(WriteOutcome.ABORTED)
