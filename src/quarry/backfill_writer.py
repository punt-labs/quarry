"""How backfill files a transcript: through whoever owns the store's writes."""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, Self

from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from datetime import datetime

    from quarry.backfill_session import BackfillSession


class WriteOutcome(Enum):
    """Why a transcript was not stored, as far as the run's next step cares."""

    FAILED = auto()  # this transcript only; the run goes on
    UNINDEXABLE = auto()  # nothing to index in it; storing it again changes nothing
    NOT_ADMITTED = auto()  # the queue had no room within the wait
    ABORTED = auto()  # the run cannot go on: shutting down, worker or loop gone


class BackfillWriteError(RefusalError):
    """A transcript was not stored; the message is the writer's URL-free reason."""

    _outcome: WriteOutcome = WriteOutcome.FAILED

    def as_outcome(self, outcome: WriteOutcome) -> Self:
        """Say which kind of not-stored this is; return the error, ready to raise."""
        self._outcome = outcome
        return self

    @property
    def outcome(self) -> WriteOutcome:
        """Return what the run should make of this failure."""
        return self._outcome


class TranscriptWriter(Protocol):
    """The one way backfill writes: the daemon's collection FIFO, never its own store.

    A second writer to a collection interleaves its delete and insert with the
    collection's worker, so backfill owns no connection to write through.
    """

    def write(
        self, collection: str, session: BackfillSession, snapshot: datetime
    ) -> None:
        """Store *session* in *collection* as it read at *snapshot*.

        Raises:
            BackfillWriteError: The transcript was not stored; the message says why
                and :attr:`~BackfillWriteError.outcome` says what the run should do.
        """
        ...
