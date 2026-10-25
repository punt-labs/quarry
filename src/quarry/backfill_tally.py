"""The counters one backfill run keeps as it goes, and the reason it stopped."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_stats import BackfillStats
from quarry.run_halt import RunHalt

if TYPE_CHECKING:
    from quarry.stop_signal import StopSignal


@final
class BackfillTally:
    """What a run has counted so far; nothing outside it changes a count.

    Every transcript ends in exactly one counter.  ``processed`` counts the
    transcripts attempted for writing (or, in a dry run, that would be): the
    number ``--limit`` caps.  A transcript that is skipped, empty, unreadable or
    unindexable never consumes it.  A run is halted by its stop signal, by an
    :meth:`abort`, or when the queue stays full for
    :attr:`~quarry.admission_streak.AdmissionStreak.MAX` transcripts in a row.
    """

    __slots__ = (
        "_errors",
        "_failed",
        "_halt",
        "_ingested",
        "_not_attempted",
        "_not_examined",
        "_processed",
        "_skipped_empty",
        "_skipped_existing",
        "_skipped_unreadable",
        "_would_ingest",
    )

    _errors: list[str]
    _failed: int
    _ingested: int
    _not_attempted: int
    _not_examined: int
    _processed: int
    _skipped_empty: int
    _skipped_existing: int
    _skipped_unreadable: int
    _halt: RunHalt
    _would_ingest: int

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._halt = RunHalt(stop)
        self._errors = []
        self._failed = self._ingested = self._would_ingest = 0
        self._not_attempted = self._not_examined = self._processed = 0
        self._skipped_empty = self._skipped_existing = self._skipped_unreadable = 0
        return self

    @property
    def processed(self) -> int:
        """Return how many transcripts were attempted (what ``--limit`` caps)."""
        return self._processed

    def stored(self) -> None:
        """Count a transcript the writer stored; the queue answered it."""
        self._ingested += 1
        self._processed += 1
        self._halt.answered()

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
        self.fail_unread(line)
        self._processed += 1

    def fail_unread(self, line: str) -> None:
        """Count a transcript that could not be read; it was never attempted.

        It is a failure, with *line* as why, but it does not consume ``--limit``.
        """
        self._errors.append(line)
        self._failed += 1

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
        return self._halt.halted()

    @property
    def halt(self) -> RunHalt:
        """Return the run's decision to stop: its reason, and the full-queue streak."""
        return self._halt

    def stats(self, skipped_unregistered: int) -> BackfillStats:
        """Return the run's stats, the stop (if any) first among the errors."""
        reason = self._halt.reason
        stop = [f"backfill stopped: {reason}"] if reason else []
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
            stopped=reason,
            errors=[*stop, *self._errors],
        )
