"""How backfill files a transcript: through whoever owns the store's writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from quarry.backfill_session import BackfillSession


class TranscriptWriter(Protocol):
    """The one way backfill writes: the daemon's collection FIFO, never its own store.

    A second writer to a collection interleaves its delete and insert with the
    collection's worker, so backfill owns no connection to write through.
    """

    def write(
        self,
        collection: str,
        session: BackfillSession,
        snapshot: datetime,
        version: str = "",
    ) -> None:
        """Store *session* in *collection* as it read at *snapshot*, at *version*.

        *version* is what the transcript was before it was read; the stored row
        carries it, so a later run can tell a session that grew from one that did
        not.

        Raises:
            BackfillWriteError: The transcript was not stored; the message says why
                and :attr:`~BackfillWriteError.outcome` says what the run should do.
        """
        ...
