"""Fail the jobs a drain-timeout abort cuts off, spooling those that need it."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.config import Settings
    from quarry.daemon.abort_reason import AbortTally
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

    - every job reaches a terminal state the moment it is taken: ``failed``, with
      a provisional reason, replaced by the true one when its spool write ends
      (a job that was withdrawn, is past its commit or already stored keeps what
      it is, and one that stores after the abort is ``completed`` again, because
      its content is stored whatever the abort said);
    - every job is counted in exactly one term of the tally, once its outcome is
      known, and :attr:`pending` says how many are not yet known, so a summary
      can still add up;
    - one job's failure to decide its outcome costs only that job, recorded as
      lost.

    Deciding an outcome can spool the job, and spooling syncs files, so there are
    two ways in: :meth:`fail` runs the syncs in the caller, and the ``async``
    ones run each job's on a daemon thread (:class:`DaemonThreads`) so a stalled
    disk can hold neither the event loop nor the process.  State and the tally
    are only touched on the caller's thread (or the loop), after a job's syncs.
    """

    UNFINISHED: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool unfinished at shutdown, "
        "content may or may not be saved",
        unfinished=True,
    )
    UNBUILT: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool record could not be built, "
        "content not recovered",
        lost=True,
    )
    NOT_WRITTEN: ClassVar[Aborted] = Aborted(
        "daemon shut down before ingest; spool write failed, content not recovered",
        lost=True,
    )

    __slots__ = ("_pending", "_reason", "_tally", "_threads")

    _pending: int
    _reason: AbortReason
    _tally: AbortTally
    _threads: DaemonThreads

    def __new__(cls, settings: Settings, tally: AbortTally) -> Self:
        self = super().__new__(cls)
        self._pending = 0
        self._reason = AbortReason.for_settings(settings)
        self._tally = tally
        self._threads = DaemonThreads.shared()
        return self

    @property
    def pending(self) -> int:
        """Return how many taken jobs have no recorded outcome yet."""
        return self._pending

    def fail(self, items: Sequence[QueuedJob]) -> None:
        """Spool and fail *items* here and now (a synchronous teardown)."""
        self._begin(items)
        for item in items:
            self._record(item, self._outcome(item))

    async def fail_off_loop(self, items: Sequence[QueuedJob]) -> None:
        """Fail *items*, each spooled on its own thread and recorded as it ends."""
        self._begin(items)
        await asyncio.gather(*(self._one_off_loop(item) for item in items))

    async def fail_in_flight(self, item: QueuedJob) -> None:
        """Spool and fail the in-flight *item*, waiting no longer than the policy.

        The drain timeout cancels the worker, then the abort cancels it again
        while it is still spooling, so the wait is re-entered after a cancel until
        the spool thread ends or :attr:`ShutdownPolicy.SPOOL_WAIT_S` passes.  A
        spool that overruns is recorded as unfinished and left to end on its own;
        if it then succeeds, the record is corrected (:meth:`_ended_late`).  One
        that raises or is cancelled is recorded as lost.  Never raises, so the
        cancel the caller is handling still propagates.
        """
        self._begin([item])
        spooling = asyncio.ensure_future(
            self._threads.run(partial(self._outcome, item))
        )
        outcome = await self.await_spool(spooling, item)
        self._record(item, outcome)
        if outcome is self.UNFINISHED:
            spooling.add_done_callback(partial(self._ended_late, item))

    async def await_spool(
        self, spooling: asyncio.Future[Aborted], item: QueuedJob
    ) -> Aborted:
        """Return the spool's outcome, or why there is none, within the deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ShutdownPolicy.SPOOL_WAIT_S
        while not spooling.done() and loop.time() < deadline:
            try:
                await asyncio.wait({spooling}, timeout=deadline - loop.time())
            except asyncio.CancelledError:
                continue  # a repeated cancel never abandons the record
        if not spooling.done():
            return self.UNFINISHED
        if spooling.cancelled():
            return self.NOT_WRITTEN
        if (failure := spooling.exception()) is not None:
            logger.error(
                "abort: spooling a %s job for %s failed",
                type(item.job).__name__,
                item.job.collection,
                exc_info=RedactedFailure.of(failure),
            )
            return self.NOT_WRITTEN
        return spooling.result()

    def _ended_late(self, item: QueuedJob, spooling: asyncio.Future[Aborted]) -> None:
        """Done callback: a spool the abort stopped waiting for has ended.

        On the loop.  A write that succeeded corrects the job's reason and moves it
        from the unfinished term to the one it really belongs to; one that failed
        or was cancelled is logged, and the job stays unfinished.
        """
        kind, collection = type(item.job).__name__, item.job.collection
        if spooling.cancelled():
            logger.warning(
                "abort: a late spool of a %s job for %s was cancelled", kind, collection
            )
        elif (failure := spooling.exception()) is not None:
            logger.error(
                "abort: a late spool of a %s job for %s failed",
                kind,
                collection,
                exc_info=RedactedFailure.of(failure),
            )
        else:
            outcome = spooling.result()
            JobLifecycle(item.state).fail_aborted(f"{outcome.reason} (after the wait)")
            self._tally.replace(self.UNFINISHED, outcome)
            logger.info(
                "abort: a %s job for %s finished spooling after the abort wait",
                kind,
                collection,
            )

    async def _one_off_loop(self, item: QueuedJob) -> None:
        """Spool one job on a daemon thread and record it on the loop when it ends."""
        try:
            outcome = await self._threads.run(partial(self._outcome, item))
        except Exception as exc:
            logger.error(
                "abort: spooling a %s job for %s failed",
                type(item.job).__name__,
                item.job.collection,
                exc_info=RedactedFailure.of(exc),
            )
            outcome = self.NOT_WRITTEN
        self._record(item, outcome)

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

    def _begin(self, items: Sequence[QueuedJob]) -> None:
        """Take *items*: each is terminal now, and pending until its outcome is in."""
        for item in items:
            JobLifecycle(item.state).fail_aborted(self.UNFINISHED.reason)
        self._pending += len(items)

    def _record(self, item: QueuedJob, aborted: Aborted) -> None:
        """Fail *item* with its true reason and count it."""
        JobLifecycle(item.state).fail_aborted(aborted.reason)
        self._tally.record(aborted)
        self._pending -= 1
