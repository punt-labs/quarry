"""Fail the jobs a drain-timeout abort cuts off, spooling those that need it."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_record import AbortRecord
from quarry.daemon.abort_tally import AbortTally
from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.spool_discard import SpoolDiscard
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.config import Settings
    from quarry.daemon.queued_unit import QueuedUnit

logger = logging.getLogger(__name__)


@final
class JobAbort:
    """Spool the jobs an abort took, off the loop, and hand each outcome on.

    Three things always hold, whatever the disk does:

    - every job the abort may fail is ``failed`` the moment it is taken, with a
      provisional reason that its outcome replaces (:class:`AbortRecord`); a job
      that was withdrawn, stored or is past its commit is left as it is;
    - every job is in exactly one term of the tally, once its outcome is known,
      and :attr:`pending` says how many are not;
    - one job's failure to decide its outcome costs only that job, recorded as
      not written.

    Deciding an outcome can spool the job, and spooling syncs files, so there are
    two ways in: :meth:`fail` runs the syncs in the caller, and the ``async``
    ones run each job's on a daemon thread (:class:`DaemonThreads`) so a stalled
    disk can hold neither the event loop nor the process.
    """

    __slots__ = ("_discard", "_reason", "_record", "_respooling", "_tally", "_threads")

    _discard: SpoolDiscard
    _reason: AbortReason
    _record: AbortRecord
    _respooling: set[asyncio.Future[None]]
    _tally: AbortTally
    _threads: DaemonThreads

    def __new__(cls, settings: Settings, tally: AbortTally) -> Self:
        self = super().__new__(cls)
        self._reason = AbortReason.for_settings(settings)
        self._respooling = set()
        self._tally = tally
        self._threads = DaemonThreads.shared()
        self._discard = SpoolDiscard(self._threads)
        self._record = AbortRecord(
            self._reason, tally, self._discard, self._respool_in_background
        )
        return self

    @property
    def tally(self) -> AbortTally:
        """Return how the jobs this abort took ended."""
        return self._tally

    @property
    def taken(self) -> tuple[QueuedUnit, ...]:
        """Return every job this abort has taken."""
        return self._record.taken

    @property
    def discard(self) -> SpoolDiscard:
        """Return the removals of spool records this abort has under way."""
        return self._discard

    @property
    def pending(self) -> int:
        """Return how many taken jobs have no recorded outcome yet."""
        return self._tally.pending

    def fail(self, items: Sequence[QueuedUnit]) -> None:
        """Spool and fail *items* here and now (a synchronous teardown)."""
        for item in self._record.take(items):
            self._record.record(item, self._outcome(item))

    async def fail_off_loop(self, items: Sequence[QueuedUnit]) -> None:
        """Fail *items*, each spooled on its own thread and recorded as it ends.

        A cancel waits for every job to record itself *unfinished* before it
        propagates, so a job a cancelled abort leaves behind is never pending.
        """
        async with asyncio.TaskGroup() as group:
            for item in self._record.take(items):
                group.create_task(self._one_off_loop(item))

    async def fail_in_flight(self, item: QueuedUnit) -> None:
        """Spool and fail the in-flight *item*, waiting no longer than the policy.

        The drain timeout cancels the worker, then the abort cancels it again
        while it is still spooling, so the wait is re-entered after a cancel until
        the spool thread ends or :attr:`ShutdownPolicy.SPOOL_WAIT_S` passes.  A
        spool that overruns is recorded as unfinished and left to end on its own;
        if it then succeeds, the record is corrected.  One that raises is
        recorded as not written.  Never raises, so the cancel the caller is handling
        still propagates.
        """
        if self._record.take([item]):
            await self._decide(item)

    async def _decide(self, item: QueuedUnit) -> None:
        """Spool the failed *item* and record the outcome, within the deadline."""
        spooling = self._spool(item)
        outcome = await self.await_spool(spooling, item)
        self._record.record(item, outcome)
        if outcome is AbortReason.UNFINISHED:
            spooling.add_done_callback(partial(self._record.late, item))

    def _respool_in_background(self, item: QueuedUnit) -> None:
        """Spool *item* again, on the loop in the background, once its store failed."""
        respooling = asyncio.ensure_future(self._decide(item))
        self._respooling.add(respooling)
        respooling.add_done_callback(partial(self._respooled, item))

    def _respooled(self, item: QueuedUnit, respooling: asyncio.Future[None]) -> None:
        """Done callback: forget the respool, and say so if it died of anything."""
        self._respooling.discard(respooling)
        if not respooling.cancelled() and (failure := respooling.exception()):
            item.log_failure("respooling", failure)

    async def await_spool(
        self, spooling: asyncio.Future[Aborted], item: QueuedUnit
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
            return AbortReason.UNFINISHED
        if (failure := spooling.exception()) is not None:
            item.log_failure("spooling", failure)
            return AbortReason.NOT_WRITTEN
        return spooling.result()

    async def _one_off_loop(self, item: QueuedUnit) -> None:
        """Spool one job on a daemon thread and record it on the loop when it ends.

        A cancel of the wait records the job *unfinished* and leaves the spool
        thread's future, which the wait only shields, to correct it when it ends.
        """
        spooling = self._spool(item)
        try:
            outcome = await asyncio.shield(spooling)
        except asyncio.CancelledError:
            self._record.record(item, AbortReason.UNFINISHED)
            spooling.add_done_callback(partial(self._record.late, item))
            raise
        except Exception as exc:
            logger.error(
                "abort: spooling a %s job for %s failed",
                type(item.job).__name__,
                item.job.collection,
                exc_info=RedactedFailure.of(exc),
            )
            outcome = AbortReason.NOT_WRITTEN
        self._record.record(item, outcome)

    def _spool(self, item: QueuedUnit) -> asyncio.Future[Aborted]:
        """Start deciding *item*'s outcome on a daemon thread."""
        return asyncio.ensure_future(self._threads.run(partial(self._outcome, item)))

    def _outcome(self, item: QueuedUnit) -> Aborted:
        """Return how *item* ends, spooling it if it needs it (blocking, total).

        Never raises: whatever goes wrong deciding this one job is logged with the
        job's kind and collection and becomes a not-written outcome, so no other job is
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
            return AbortReason.UNBUILT
