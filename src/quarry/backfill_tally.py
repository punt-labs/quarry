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
    :attr:`~quarry.run_halt.RunHalt.MAX_FULL` transcripts in a row.
    """

    __slots__ = ("_counts", "_errors", "_halt")

    _counts: dict[str, int]
    _errors: list[str]
    _halt: RunHalt

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._halt = RunHalt(stop)
        self._errors = []
        self._counts = {}
        return self

    @property
    def processed(self) -> int:
        """Return how many transcripts were attempted (what ``--limit`` caps)."""
        return self._counts.get("processed", 0)

    def stored(self) -> None:
        """Count a transcript the writer stored; the queue answered it."""
        self._attempt("ingested")
        self._halt.answered()

    def stored_unconfirmed(self) -> None:
        """Count a transcript whose job was past its commit when the run gave up.

        The store may have landed or failed; it is neither stored nor failed.
        """
        self._attempt("stored_unconfirmed")

    def truncated(self) -> None:
        """Count a transcript whose oldest records the record bound dropped."""
        self._bump("truncated")

    def unlocked_capture(self) -> None:
        """Count a capture file written without its lock (a filesystem that cannot)."""
        self._bump("unlocked_captures")

    def would_store(self) -> None:
        """Count a transcript a dry run would have stored."""
        self._attempt("would_ingest")

    def skip_existing(self) -> None:
        """Count a transcript whose stored copy is already current."""
        self._bump("skipped_existing")

    def skip_empty(self) -> None:
        """Count a transcript with nothing to index."""
        self._bump("skipped_empty")

    def skip_unreadable(self) -> None:
        """Count a transcript that vanished before it could be read."""
        self._bump("skipped_unreadable")

    def skip_refused(self, line: str) -> None:
        """Count a transcript refused for what it is, with *line* as the note why.

        It is neither empty nor failed, and the note is kept every run: a refusal
        is re-detected from the file's own stat, never remembered.
        """
        self._bump("skipped_refused")
        self._errors.append(line)

    def fail(self, line: str) -> None:
        """Count an attempted transcript that was not stored; keep *line* as why."""
        self.fail_unread(line)
        self._bump("processed")

    def fail_unread(self, line: str) -> None:
        """Count a transcript that could not be read; it was never attempted.

        It is a failure, with *line* as why, but it does not consume ``--limit``.
        """
        self._errors.append(line)
        self._bump("failed")

    def note(self, line: str) -> None:
        """Keep *line* among the run's errors without counting a transcript."""
        self._errors.append(line)

    def leave(self, count: int) -> None:
        """Count *count* transcripts that still needed work when the run stopped."""
        self._bump("not_attempted", count)

    def leave_unexamined(self, count: int) -> None:
        """Count *count* transcripts in projects the run never looked into."""
        self._bump("not_examined", count)

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
        counts = self._counts
        return BackfillStats(
            ingested=counts.get("ingested", 0),
            would_ingest=counts.get("would_ingest", 0),
            skipped_existing=counts.get("skipped_existing", 0),
            skipped_unregistered=skipped_unregistered,
            skipped_empty=counts.get("skipped_empty", 0),
            skipped_unreadable=counts.get("skipped_unreadable", 0),
            skipped_refused=counts.get("skipped_refused", 0),
            truncated=counts.get("truncated", 0),
            unlocked_captures=counts.get("unlocked_captures", 0),
            failed=counts.get("failed", 0),
            stored_unconfirmed=counts.get("stored_unconfirmed", 0),
            not_attempted=counts.get("not_attempted", 0),
            not_examined=counts.get("not_examined", 0),
            stopped=reason,
            errors=[*stop, *self._errors],
        )

    def _bump(self, counter: str, by: int = 1) -> None:
        """Add *by* to *counter*."""
        self._counts[counter] = self._counts.get(counter, 0) + by

    def _attempt(self, counter: str) -> None:
        """Count an attempted transcript under *counter*; it consumes ``--limit``."""
        self._bump(counter)
        self._bump("processed")
