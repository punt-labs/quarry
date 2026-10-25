"""The counters one backfill run keeps as it goes, and the reason it stopped."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.backfill_stats import BackfillStats
from quarry.backfill_writer import WriteOutcome

if TYPE_CHECKING:
    from quarry.stop_signal import StopSignal


@final
class BackfillTally:
    """What a run has counted so far; nothing outside it changes a count.

    Every transcript ends in exactly one counter.  ``processed`` counts the
    transcripts attempted for writing (or, in a dry run, that would be): the
    number ``--limit`` caps.  A transcript that is skipped, empty, unreadable or
    unindexable never consumes it.  A run is halted by its stop signal, by an
    :meth:`abort`, or when the queue stays full for :attr:`MAX_UNADMITTED`
    transcripts in a row.
    """

    # Consecutive "queue stayed full" outcomes after which the run gives up: the
    # queue is not draining, and each further transcript would wait out the same
    # deadline for the same answer.
    MAX_UNADMITTED: ClassVar[int] = 3

    __slots__ = (
        "_abort",
        "_errors",
        "_failed",
        "_ingested",
        "_not_attempted",
        "_not_examined",
        "_processed",
        "_skipped_empty",
        "_skipped_existing",
        "_skipped_unreadable",
        "_stop",
        "_unadmitted",
        "_would_ingest",
    )

    _abort: str
    _errors: list[str]
    _failed: int
    _ingested: int
    _not_attempted: int
    _not_examined: int
    _processed: int
    _skipped_empty: int
    _skipped_existing: int
    _skipped_unreadable: int
    _stop: StopSignal
    _unadmitted: int
    _would_ingest: int

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._abort = ""
        self._errors = []
        self._failed = self._ingested = self._would_ingest = 0
        self._not_attempted = self._not_examined = self._processed = 0
        self._skipped_empty = self._skipped_existing = self._skipped_unreadable = 0
        self._unadmitted = 0
        return self

    @property
    def processed(self) -> int:
        """Return how many transcripts were attempted (what ``--limit`` caps)."""
        return self._processed

    def stored(self) -> None:
        """Count a transcript the writer stored; the queue answered it."""
        self._ingested += 1
        self._processed += 1
        self._unadmitted = 0

    def would_store(self) -> None:
        """Count a transcript a dry run would have stored."""
        self._would_ingest += 1
        self._processed += 1

    def skip_existing(self) -> None:
        """Count a transcript whose stored copy is already current."""
        self._skipped_existing += 1

    def skip_empty(self) -> None:
        """Count a transcript with nothing to index."""
        self._skipped_empty += 1

    def skip_unreadable(self) -> None:
        """Count a transcript that vanished before it could be read."""
        self._skipped_unreadable += 1

    def fail(self, line: str) -> None:
        """Count an attempted transcript that was not stored; keep *line* as why."""
        self._errors.append(line)
        self._failed += 1
        self._processed += 1

    def note(self, line: str) -> None:
        """Keep *line* among the run's errors without counting a transcript."""
        self._errors.append(line)

    def leave(self, count: int) -> None:
        """Count *count* transcripts that still needed work when the run stopped."""
        self._not_attempted += count

    def leave_unexamined(self, count: int) -> None:
        """Count *count* transcripts in projects the run never looked into."""
        self._not_examined += count

    def halted(self) -> bool:
        """Return whether the run must stop before its next step."""
        if self._stop.is_set():
            self.abort(self._stop.reason)
        return bool(self._abort)

    def abort(self, reason: str) -> None:
        """Stop the run for *reason* (the first one given is kept)."""
        self._abort = self._abort or reason

    def answered(self) -> None:
        """Note that a transcript went through the queue and was answered."""
        self._unadmitted = 0

    def refused(self, outcome: WriteOutcome, reason: str) -> None:
        """Note how the queue answered a transcript it did not store.

        A queue that stayed full extends the streak; a job that had started when
        the run gave up stops the run; any other answer ends the streak.
        """
        if outcome is WriteOutcome.NOT_ADMITTED:
            self.not_admitted()
        elif outcome is WriteOutcome.ABORTED_RUNNING:
            self.abort(reason)
        else:
            self.answered()

    def not_admitted(self) -> None:
        """Note a transcript the queue would not take; give up if they keep coming."""
        self._unadmitted += 1
        if self._unadmitted >= self.MAX_UNADMITTED:
            self.abort("ingest queue stayed full")

    def stats(self, skipped_unregistered: int) -> BackfillStats:
        """Return the run's stats, the stop (if any) first among the errors."""
        stop = [f"backfill stopped: {self._abort}"] if self._abort else []
        return BackfillStats(
            ingested=self._ingested,
            would_ingest=self._would_ingest,
            skipped_existing=self._skipped_existing,
            skipped_unregistered=skipped_unregistered,
            skipped_empty=self._skipped_empty,
            skipped_unreadable=self._skipped_unreadable,
            failed=self._failed,
            not_attempted=self._not_attempted,
            not_examined=self._not_examined,
            stopped=self._abort,
            errors=[*stop, *self._errors],
        )
