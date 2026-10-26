"""One record of how each job a shutdown abort took ended, and the counts it implies."""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterable


class AbortTerm(Enum):
    """The one term of the tally a taken job is counted in.

    ``PENDING``: taken, outcome not yet known.  ``SPOOLED``: a record was
    written and can be run again; ``UNRECOVERABLE``: a record was written but
    only as a note.  ``LOST``: the spool write failed or the record could not be
    built, so the content is gone.  ``UNFINISHED``: the write was still running
    when the abort stopped waiting, so the content may or may not be on disk.
    ``NO_SPOOL``: the job is run again from its source, or a durable copy exists.
    ``KEPT``: the abort left the job as it is: withdrawn, aborted by its
    producer, or stored.  ``UNCONFIRMED``: a committing job whose store has not
    ended.
    """

    PENDING = auto()
    SPOOLED = auto()
    UNRECOVERABLE = auto()
    LOST = auto()
    UNFINISHED = auto()
    NO_SPOOL = auto()
    KEPT = auto()
    UNCONFIRMED = auto()


@final
class AbortTally:
    """A ledger: every job taken is in exactly one term, and the counts follow.

    The counts are *derived* from the ledger, never kept beside it, so the terms
    add up to the jobs taken by construction.  A job changes term in one place,
    :meth:`move`.  The ledger is touched only on the event loop (or the caller of
    a synchronous teardown), after a job's blocking work has ended.
    """

    __slots__ = ("_ledger",)

    # Keyed by the job's identity; the job is held so the key cannot be reused.
    _ledger: dict[int, tuple[object, AbortTerm]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._ledger = {}
        return self

    @classmethod
    def total(cls, tallies: Iterable[AbortTally]) -> Self:
        """Return one tally that counts everything *tallies* counted."""
        combined = cls()
        for tally in tallies:
            combined._ledger.update(tally._ledger)
        return combined

    @property
    def pending(self) -> int:
        """Return how many taken jobs have no recorded outcome yet."""
        return self.count(AbortTerm.PENDING)

    def take(self, job: object) -> bool:
        """Count *job* as pending; return False if it was already taken."""
        if id(job) in self._ledger:
            return False
        self._ledger[id(job)] = (job, AbortTerm.PENDING)
        return True

    def move(self, job: object, term: AbortTerm) -> None:
        """Move the taken *job* to *term*."""
        self._ledger[id(job)] = (job, term)

    def term_of(self, job: object) -> AbortTerm | None:
        """Return the term *job* is in, or ``None`` if it was never taken."""
        entry = self._ledger.get(id(job))
        return entry[1] if entry else None  # absence: the job was never taken

    def count(self, term: AbortTerm) -> int:
        """Return how many jobs are in *term*."""
        return sum(1 for _, held in self._ledger.values() if held is term)

    def summary(self) -> str:
        """Return the counts as words; the terms sum to the job count."""
        settled = len(self._ledger) - self.pending
        term = self.count
        unfinished = term(AbortTerm.UNFINISHED)
        return (
            f"{settled} job(s): {term(AbortTerm.SPOOLED)} spooled for recovery, "
            f"{term(AbortTerm.UNRECOVERABLE)} spooled but not recoverable, "
            f"{term(AbortTerm.LOST)} lost (spool write failed), "
            f"{f'{unfinished} spool unfinished at shutdown, ' if unfinished else ''}"
            f"{term(AbortTerm.NO_SPOOL)} needed no spool, "
            f"{term(AbortTerm.KEPT)} kept (withdrawn, aborted or stored), "
            f"{term(AbortTerm.UNCONFIRMED)} unconfirmed (committing at shutdown)"
        )
