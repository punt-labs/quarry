"""Fail the jobs a drain-timeout abort cuts off, spooling those that need it."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_tally import AbortTerm
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.config import Settings
    from quarry.daemon.abort_tally import AbortTally
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class QueuedJob:
    """One admitted job paired with the task state it reports through."""

    job: IngestUnit
    state: TaskState


@final
class JobAbort:
    """Fail aborted jobs with their truthful reason, and count how each ended.

    Three things always hold, whatever the disk does:

    - every job the abort may fail is ``failed`` the moment it is taken, with a
      provisional reason that its spool write replaces when it ends; a job that
      was withdrawn, aborted by its producer or already stored is left as it is
      and counted *kept*, and one past its commit is left ``committing`` and
      counted *unconfirmed*;
    - every job is in exactly one term of the tally (:class:`AbortTally`), and
      the outcome of a job moves its reason and its term together, once: it is
      recorded only while the job is still pending;
    - one job's failure to decide its outcome costs only that job, recorded as
      lost.

    Deciding an outcome can spool the job, and spooling syncs files, so there are
    two ways in: :meth:`fail` runs the syncs in the caller, and the ``async``
    ones run each job's on a daemon thread (:class:`DaemonThreads`) so a stalled
    disk can hold neither the event loop nor the process.  The tally is only
    touched on the caller's thread (or the loop), after a job's syncs.
    """

    UNFINISHED: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool unfinished at shutdown, "
        "content may or may not be saved",
        AbortTerm.UNFINISHED,
    )
    UNBUILT: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool record could not be built, "
        "content not recovered",
        AbortTerm.LOST,
    )
    NOT_WRITTEN: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool write failed, content not recovered",
        AbortTerm.LOST,
    )

    __slots__ = ("_reason", "_tally", "_threads")

    _reason: AbortReason
    _tally: AbortTally
    _threads: DaemonThreads

    def __new__(cls, settings: Settings, tally: AbortTally) -> Self:
        self = super().__new__(cls)
        self._reason = AbortReason.for_settings(settings)
        self._tally = tally
        self._threads = DaemonThreads.shared()
        return self

    @property
    def pending(self) -> int:
        """Return how many taken jobs have no recorded outcome yet."""
        return self._tally.pending

    def fail(self, items: Sequence[QueuedJob]) -> None:
        """Spool and fail *items* here and now (a synchronous teardown)."""
        for item in self._begin(items):
            self._record(item, self._outcome(item))

    async def fail_off_loop(self, items: Sequence[QueuedJob]) -> None:
        """Fail *items*, each spooled on its own thread and recorded as it ends.

        A cancel waits for every job to record itself *unfinished* before it
        propagates, so a job a cancelled abort leaves behind is never pending.
        """
        async with asyncio.TaskGroup() as group:
            for item in self._begin(items):
                group.create_task(self._one_off_loop(item))

    async def fail_in_flight(self, item: QueuedJob) -> None:
        """Spool and fail the in-flight *item*, waiting no longer than the policy.

        The drain timeout cancels the worker, then the abort cancels it again
        while it is still spooling, so the wait is re-entered after a cancel until
        the spool thread ends or :attr:`ShutdownPolicy.SPOOL_WAIT_S` passes.  A
        spool that overruns is recorded as unfinished and left to end on its own;
        if it then succeeds, the record is corrected (:meth:`_ended_late`).  One
        that raises is recorded as lost.  Never raises, so the cancel the caller
        is handling still propagates.
        """
        if not self._begin([item]):
            return
        spooling = self._spool(item)
        outcome = await self.await_spool(spooling, item)
        self._record(item, outcome)
        if outcome is self.UNFINISHED:
            spooling.add_done_callback(partial(self._ended_late, item))

    async def await_spool(
        self, spooling: asyncio.Future[Aborted], item: QueuedJob
    ) -> Aborted:
        """Return the spool's outcome, or why there is none, within the deadline.

        A spool future that was cancelled while its thread may still write is
        *unfinished*, not a failed write: what reaches disk is not known.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ShutdownPolicy.SPOOL_WAIT_S
        while not spooling.done() and loop.time() < deadline:
            try:
                await asyncio.wait({spooling}, timeout=deadline - loop.time())
            except asyncio.CancelledError:
                continue  # a repeated cancel never abandons the record
        if not spooling.done() or spooling.cancelled():
            return self.UNFINISHED
        if (failure := spooling.exception()) is not None:
            self._log_failure("spooling", item, failure)
            return self.NOT_WRITTEN
        return spooling.result()

    def _ended_late(self, item: QueuedJob, spooling: asyncio.Future[Aborted]) -> None:
        """Done callback: a spool the abort stopped waiting for has ended.

        On the loop.  A write that succeeded corrects the job's reason and its
        term together, but only while the job is still *unfinished*; one that
        failed or was cancelled is logged, and the job stays unfinished.
        """
        if self._tally.term_of(item) is not AbortTerm.UNFINISHED:
            return
        kind, collection = type(item.job).__name__, item.job.collection
        if spooling.cancelled():
            logger.warning(
                "abort: a late spool of a %s job for %s was cancelled", kind, collection
            )
        elif (failure := spooling.exception()) is not None:
            self._log_failure("a late spool of", item, failure)
        else:
            outcome = spooling.result()
            self._settle(item, outcome, f"{outcome.reason} (after the wait)")
            logger.info(
                "abort: a %s job for %s finished spooling after the abort wait",
                kind,
                collection,
            )

    async def _one_off_loop(self, item: QueuedJob) -> None:
        """Spool one job on a daemon thread and record it on the loop when it ends.

        A cancel of the wait records the job *unfinished* and leaves the spool
        thread's future, which the wait only shields, to correct it when it ends.
        """
        spooling = self._spool(item)
        try:
            outcome = await asyncio.shield(spooling)
        except asyncio.CancelledError:
            self._record(item, self.UNFINISHED)
            spooling.add_done_callback(partial(self._ended_late, item))
            raise
        except Exception as exc:
            logger.error(
                "abort: spooling a %s job for %s failed",
                type(item.job).__name__,
                item.job.collection,
                exc_info=RedactedFailure.of(exc),
            )
            outcome = self.NOT_WRITTEN
        self._record(item, outcome)

    def _spool(self, item: QueuedJob) -> asyncio.Future[Aborted]:
        """Start deciding *item*'s outcome on a daemon thread."""
        return asyncio.ensure_future(self._threads.run(partial(self._outcome, item)))

    def _outcome(self, item: QueuedJob) -> Aborted:
        """Return how *item* ends, spooling it if it needs it (blocking, total).

        Never raises: whatever goes wrong deciding this one job is logged with the
        job's kind and collection and becomes a lost outcome, so no other job is
        affected.
        """
        try:
            return self._reason.of(item.job)
        except Exception as exc:
            logger.error(
                "abort: deciding how a %s job for %s ends failed",
                type(item.job).__name__,
                item.job.collection,
                exc_info=RedactedFailure.of(exc),
            )
            return self.UNBUILT

    def _begin(self, items: Sequence[QueuedJob]) -> list[QueuedJob]:
        """Take *items*; return those the abort has failed and must now decide.

        A job already taken is skipped.  One it may fail is ``failed`` at once and
        pending; the producer's, a stored one and one past its commit are counted
        in the term that says so and go no further.
        """
        disposition = JobLifecycle.Disposition
        failing: list[QueuedJob] = []
        for item in items:
            if not self._tally.take(item):
                continue
            found = JobLifecycle(item.state).fail_aborted(self.UNFINISHED.reason)
            if found is disposition.FAILED:
                failing.append(item)
            else:
                self._tally.move(item, self._term_of(found, self.UNFINISHED))
        return failing

    def _record(self, item: QueuedJob, aborted: Aborted) -> None:
        """Record *item*'s outcome, unless it is no longer pending."""
        if self._tally.term_of(item) is AbortTerm.PENDING:
            self._settle(item, aborted, aborted.reason)

    def _settle(self, item: QueuedJob, aborted: Aborted, reason: str) -> None:
        """Move *item*'s reason and its term together, as its status allows."""
        found = JobLifecycle(item.state).fail_aborted(reason)
        self._tally.move(item, self._term_of(found, aborted))

    @staticmethod
    def _term_of(found: JobLifecycle.Disposition, aborted: Aborted) -> AbortTerm:
        """Return the term for a job whose status the abort found as *found*."""
        match found:
            case JobLifecycle.Disposition.KEPT:
                return AbortTerm.KEPT
            case JobLifecycle.Disposition.UNCONFIRMED:
                return AbortTerm.UNCONFIRMED
            case _:
                return aborted.term

    @staticmethod
    def _log_failure(what: str, item: QueuedJob, failure: BaseException) -> None:
        """Log that *what* failed for *item*, by the job's kind and collection."""
        logger.error(
            "abort: %s a %s job for %s failed",
            what,
            type(item.job).__name__,
            item.job.collection,
            exc_info=RedactedFailure.of(failure),
        )
