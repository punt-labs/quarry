"""How each transcript ended, counted: stored, skipped, refused or failed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_writer import BackfillWriteError, WriteOutcome
from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill_tally import BackfillTally
    from quarry.backfill_unindexable import UnindexableTranscripts
    from quarry.refusal import RefusalError
    from quarry.transcript_read import TranscriptRead

logger = logging.getLogger(__name__)


@final
class TranscriptOutcomes:
    """Keep the tally for one run as its transcripts end.

    A transcript the writer refuses ends by what the run should make of the
    refusal: an abort stops the run (a job that never ran is not attempted; one
    that had started is a failure, since it may still be stored), nothing-to-index
    is a skip, anything else is a failure.  Every outcome but the abort went
    through the queue and was answered, so it ends (or extends) the not-admitted
    streak; a failure before the queue (the capture file, a fault in the writer)
    does neither.
    """

    __slots__ = ("_dry_run", "_tally", "_unindexable")

    _tally: BackfillTally
    _unindexable: UnindexableTranscripts
    _dry_run: bool

    def __new__(
        cls,
        tally: BackfillTally,
        unindexable: UnindexableTranscripts,
        *,
        dry_run: bool,
    ) -> Self:
        self = super().__new__(cls)
        self._tally = tally
        self._unindexable = unindexable
        self._dry_run = dry_run
        return self

    def stored(self) -> None:
        """Count a transcript the writer stored."""
        self._tally.stored()

    def would_store(self) -> None:
        """Count a transcript a dry run would have stored."""
        self._tally.would_store()

    def unindexable(self, read: TranscriptRead) -> None:
        """Count a transcript with nothing to index, and remember it for later runs."""
        self._tally.skip_empty()
        if not self._dry_run:
            self._unindexable.record(read)

    def capture_failed(self, transcript: Path) -> None:
        """Count a transcript whose capture file could not be written."""
        msg = "capture file could not be written"
        logger.warning("backfill: %s not stored: %s", transcript.name, msg)
        self.failed(transcript, BackfillWriteError(msg))

    def refused(self, read: TranscriptRead, exc: RefusalError) -> None:
        """Count a transcript the writer refused, by what the refusal means."""
        outcome = (
            exc.outcome if isinstance(exc, BackfillWriteError) else WriteOutcome.FAILED
        )
        if outcome is WriteOutcome.UNINDEXABLE:
            self.unindexable(read)
            self._tally.answered()
        elif outcome is WriteOutcome.ABORTED:
            self._tally.abort(str(exc))
            self._tally.leave(1)
        else:
            logger.warning("backfill: %s not stored: %s", read.transcript.name, exc)
            self.failed(read.transcript, exc)
            self._tally.refused(outcome, str(exc))

    def failed(self, transcript: Path, exc: Exception) -> None:
        """Count an attempted transcript that was not stored."""
        self._tally.fail(FailureReason.line(transcript.name, exc))
