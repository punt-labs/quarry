"""What the abort has recorded of each job it took: its reason, its term, its file."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_reason import Aborted, AbortReason
from quarry.daemon.abort_tally import AbortTerm
from quarry.daemon.job_lifecycle import JobLifecycle

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Sequence
    from pathlib import Path

    from quarry.daemon.abort_tally import AbortTally
    from quarry.daemon.queued_unit import QueuedUnit
    from quarry.daemon.spool_discard import SpoolDiscard

logger = logging.getLogger(__name__)


@final
class AbortRecord:
    """The one place a taken job's reason and its term move, together, once.

    A job the abort may fail is ``failed`` with a provisional reason the moment
    it is taken and counted pending; its outcome replaces both, but only while it
    is still pending, so a late or repeated outcome cannot move it again.  A job
    its producer withdrew, one that stored and one past its commit are left as
    they are and counted *kept* or *unconfirmed*.  A store that lands after the
    worker was cancelled makes its job kept, and removes the record the abort
    wrote for it.  Everything here runs on the loop (or the caller of a
    synchronous teardown), after a job's blocking work has ended.
    """

    __slots__ = ("_discard", "_reason", "_tally", "_written")

    _discard: SpoolDiscard
    _reason: AbortReason
    _tally: AbortTally
    _written: dict[int, Path]

    def __new__(
        cls, reason: AbortReason, tally: AbortTally, discard: SpoolDiscard
    ) -> Self:
        self = super().__new__(cls)
        self._discard = discard
        self._reason = reason
        self._tally = tally
        self._written = {}
        return self

    def take(self, items: Sequence[QueuedUnit]) -> list[QueuedUnit]:
        """Take *items*; return those the abort has failed and must now decide.

        A job already taken is skipped.
        """
        disposition = JobLifecycle.Disposition
        failing: list[QueuedUnit] = []
        for item in items:
            if not self._tally.take(item):
                continue
            if (store := item.state.store) is not None:
                store.listen(partial(self.store_ended, item))
            found = JobLifecycle(item.state).fail_aborted(AbortReason.UNFINISHED.reason)
            if found is disposition.FAILED:
                failing.append(item)
            else:
                self._tally.move(item, self._term_of(found, AbortReason.UNFINISHED))
        return failing

    def record(self, item: QueuedUnit, aborted: Aborted) -> None:
        """Record *item*'s outcome, unless it is no longer pending.

        A job that stored while its spool ran is already counted kept; the record
        its spool wrote is then removed, not counted.
        """
        term = self._tally.term_of(item)
        if term is AbortTerm.PENDING:
            self._settle(item, aborted, aborted.reason)
        elif term is AbortTerm.KEPT:
            self._discard.discard(aborted.path)

    def late(self, item: QueuedUnit, spooling: asyncio.Future[Aborted]) -> None:
        """Done callback: a spool the abort stopped waiting for has ended.

        A write that succeeded corrects the job's reason and its term together,
        but only while the job is still *unfinished*; one that failed or was
        cancelled is logged, and the job stays unfinished.
        """
        term = self._tally.term_of(item)
        if term is AbortTerm.KEPT:  # the job stored while the spool ran
            self._discard.discard_late(spooling)
        elif term is not AbortTerm.UNFINISHED:
            return
        elif spooling.cancelled():
            logger.warning(
                "abort: a late spool of a %s job for %s was cancelled",
                type(item.job).__name__,
                item.job.collection,
            )
        elif (failure := spooling.exception()) is not None:
            item.log_failure("a late spool of", failure)
        else:
            outcome = spooling.result()
            self._settle(item, outcome, f"{outcome.reason} (after the wait)")
            logger.info(
                "abort: a %s job for %s finished spooling after the abort wait",
                type(item.job).__name__,
                item.job.collection,
            )

    def store_ended(self, item: QueuedUnit, observed: JobLifecycle.Observed) -> None:
        """Observer's listener: a store ended after the worker was cancelled.

        A store that landed makes the job *kept* and removes its spool record.
        One that failed on a committing job ends it ``failed``, to be run again
        from its source.  Nothing else changes.
        """
        term = self._tally.term_of(item)
        if observed is JobLifecycle.Observed.COMPLETED and term is not None:
            self._tally.move(item, AbortTerm.KEPT)
            self._discard.discard(self._written.pop(id(item), None))
        elif observed is JobLifecycle.Observed.FAILED and term is AbortTerm.UNCONFIRMED:
            outcome = self._reason.without_spool(item.job)
            JobLifecycle(item.state).fail_aborted(outcome.reason)
            self._tally.move(item, outcome.term)

    def _settle(self, item: QueuedUnit, aborted: Aborted, reason: str) -> None:
        """Move *item*'s reason and its term together, as its status allows."""
        found = JobLifecycle(item.state).fail_aborted(reason)
        self._tally.move(item, self._term_of(found, aborted))
        if found is JobLifecycle.Disposition.FAILED and aborted.path is not None:
            self._written[id(item)] = aborted.path
        elif found is JobLifecycle.Disposition.KEPT:
            self._discard.discard(aborted.path)

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
