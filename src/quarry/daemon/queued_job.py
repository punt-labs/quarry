"""One backfilled transcript's job: submitted from a thread, and how its wait ended."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.daemon.backfill_job import BackfillTranscriptJob
from quarry.daemon.ingest_refusal import IngestRefusal, IngestRefusedError
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.thread_call import ThreadCallAbortedError
from quarry.refusal import RefusalError

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


class JobWithdrawnError(RefusalError):
    """The job's producer withdrew it before it committed; nothing was written."""


@final
class QueuedJob:
    """A transcript's job, from its submission until the store answers or the wait ends.

    The thread that submitted it owns the state the job reports through, so when
    the wait ends without an answer the job is withdrawn if it has not committed
    (its worker then skips it, or it stops at its commit: nothing the run gave up
    on is written later), and the failure says how far it had got.

    The ingest pipeline reports :attr:`COMMIT_POINT` once it holds its chunks and
    before it replaces the earlier document or inserts anything.  Reaching it
    claims the commit: a job whose producer already withdrew it raises, so
    nothing is deleted or stored.  A job past it can no longer be withdrawn, but
    the store can still fail (it deletes the earlier copy, then inserts), so a
    producer that stops waiting for a committing job is told it is *unconfirmed*:
    neither stored nor lost.  ``tests/test_commit_checkpoint.py`` pins the
    pipeline's message and its place before the delete.
    """

    COMMIT_POINT: ClassVar[str] = "Embedding + storing in bounded windows"

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
            BackfillTranscriptJob(replace(job.inner, checkpoint=self.checkpoint))
        )
        state = self._state
        if state.status == "completed":
            return
        error = BackfillWriteError(state.error)
        if state.error == RefusalAwareTerminal.NOTHING_STORED:
            error.as_outcome(WriteOutcome.UNINDEXABLE)
        raise error

    def checkpoint(self, message: str) -> None:
        """Claim the commit when *message* is the commit point; raise if withdrawn."""
        if message == self.COMMIT_POINT and not JobLifecycle(self._state).commit():
            msg = "job withdrawn before its commit"
            raise JobWithdrawnError(msg)

    def _submit(self, job: BackfillTranscriptJob) -> None:
        """Run *job* on the loop until its state is terminal, or raise the refusal."""
        try:
            self._call.result(self._queued.run_tracked(job, self._state))
        except IngestRefusedError as exc:
            outcome = _OUTCOMES[exc.reason]
            if outcome is not WriteOutcome.ABORTED:
                raise BackfillWriteError(str(exc)).as_outcome(outcome) from exc
            self._settle(exc)
        except ThreadCallAbortedError as exc:
            self._settle(exc)

    def _settle(self, cause: Exception) -> None:
        """Return if the job finished by itself; else raise how the wait left it.

        Raises:
            BackfillWriteError: The job never started (``ABORTED``), was stopped at
                its commit (``ABORTED_RUNNING``) or is past it with its store
                unconfirmed (``ABORTED_COMMITTING``).
        """
        withdrawn = JobLifecycle.Withdrawn
        match JobLifecycle(self._state).withdraw():
            case withdrawn.NEVER_STARTED:
                outcome = WriteOutcome.ABORTED
            case withdrawn.STARTED:
                outcome = WriteOutcome.ABORTED_RUNNING
            case withdrawn.COMMITTING:
                outcome = WriteOutcome.ABORTED_COMMITTING
            case _:
                return  # it stored or failed with its own reason
        raise BackfillWriteError(str(cause)).as_outcome(outcome) from cause
