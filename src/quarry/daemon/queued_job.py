"""One backfilled transcript's job: submitted from a thread, and how its wait ended."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.aborted_job import AbortedJob
from quarry.daemon.backfill_job import BackfillTranscriptJob
from quarry.daemon.commit_checkpoint import CommitCheckpoint
from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError
from quarry.daemon.job_withdrawal import JobWithdrawal
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.thread_call import ThreadCallAbortedError

if TYPE_CHECKING:
    from quarry.daemon.queued_ingest import QueuedIngest
    from quarry.daemon.tasks import TaskState
    from quarry.daemon.thread_call import ThreadCall

# What each way the wait can end means for the backfill run.  An unfinished job
# aborts it as a dead worker does: a hung worker would otherwise collect one
# orphaned job, and one full wait, per transcript.
_OUTCOMES = {
    IngestRefusal.SHUTTING_DOWN: WriteOutcome.ABORTED,
    IngestRefusal.WORKER_STOPPED: WriteOutcome.ABORTED,
    IngestRefusal.NOT_ADMITTED: WriteOutcome.NOT_ADMITTED,
    IngestRefusal.UNFINISHED: WriteOutcome.ABORTED,
}


@final
class QueuedJob:
    """A transcript's job, from its submission until the store answers or the wait ends.

    The thread that submitted it owns the state the job reports through, so when
    the wait ends without an answer the job is withdrawn if it is still waiting
    (its worker then skips it: nothing the run gave up on is written later), and
    the failure says whether it had started.
    """

    __slots__ = ("_call", "_queued", "_state")

    _call: ThreadCall
    _queued: QueuedIngest
    _state: TaskState

    def __new__(cls, call: ThreadCall, queued: QueuedIngest) -> Self:
        self = super().__new__(cls)
        self._call = call
        self._queued = queued
        self._state = queued.new_state()
        return self

    def store(self, job: BackfillTranscriptJob) -> None:
        """Run *job* through the FIFO; raise if the worker did not store it."""
        self._submit(
            BackfillTranscriptJob(replace(job.inner, checkpoint=self._checkpoint()))
        )
        state = self._state
        if state.status in {"completed", "committing"}:
            return
        error = BackfillWriteError(state.error)
        if state.error == RefusalAwareTerminal.NOTHING_STORED:
            error.as_outcome(WriteOutcome.UNINDEXABLE)
        raise error

    def _checkpoint(self) -> CommitCheckpoint:
        """Return the checkpoint that stops this job at its commit if withdrawn."""
        return CommitCheckpoint(JobWithdrawal(self._state))

    def _submit(self, job: BackfillTranscriptJob) -> None:
        """Run *job* on the loop until its state is terminal, or raise the refusal."""
        try:
            self._call.result(self._queued.run_tracked(job, self._state))
        except IngestRefusedError as exc:
            outcome = _OUTCOMES[exc.reason]
            if outcome is not WriteOutcome.ABORTED:
                raise BackfillWriteError(str(exc)).as_outcome(outcome) from exc
            AbortedJob(self._state).settle(exc)
        except ThreadCallAbortedError as exc:
            AbortedJob(self._state).settle(exc)
