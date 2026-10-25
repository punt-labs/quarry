"""What a backfill run counted: the tally it keeps, and the stats it returns."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import ClassVar, final

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True)
class BackfillStats:
    """Aggregate counts from a backfill run.

    ``ingested`` counts transcripts stored; a dry run counts ``would_ingest``
    instead and stores nothing.  ``failed`` counts transcripts that were
    attempted and not stored; ``not_attempted`` counts those the run never
    reached (it was stopped, or their project could not be read).
    """

    ingested: int = 0
    would_ingest: int = 0
    skipped_existing: int = 0
    skipped_unregistered: int = 0
    skipped_empty: int = 0
    skipped_unreadable: int = 0
    failed: int = 0
    not_attempted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def stored_nothing(self) -> bool:
        """Return whether the run reached no transcript and reported errors."""
        return not (self.ingested or self.would_ingest) and bool(self.errors)

    def summary(self) -> str:
        """Return ``N ingested, M skipped, K failed, R not attempted``."""
        done = self.ingested or self.would_ingest
        skipped = self.skipped_existing + self.skipped_empty + self.skipped_unreadable
        return (
            f"{done} ingested, {skipped} skipped, "
            f"{self.failed} failed, {self.not_attempted} not attempted"
        )

    def log(self, *, dry_run: bool) -> None:
        """Log the run's summary: always one line, a WARNING if it fell short."""
        label = "backfill (dry run)" if dry_run else "backfill"
        level = logging.WARNING if self.failed + self.not_attempted else logging.INFO
        logger.log(level, "%s: %s", label, self.summary())

    def as_results(self, *, dry_run: bool) -> dict[str, object]:
        """Return the stats as a task's results (a dry run says ``would_ingest``)."""
        stored = (
            {"would_ingest": self.would_ingest}
            if dry_run
            else {"ingested": self.ingested}
        )
        return {
            **stored,
            "skipped_existing": self.skipped_existing,
            "skipped_unregistered": self.skipped_unregistered,
            "skipped_empty": self.skipped_empty,
            "skipped_unreadable": self.skipped_unreadable,
            "failed": self.failed,
            "not_attempted": self.not_attempted,
            "errors": list(self.errors),
            "dry_run": dry_run,
        }


@final
@dataclass(slots=True)
class BackfillTally:
    """The counters a run keeps as it goes, and the reason it stopped, if it did.

    ``processed`` counts transcripts attempted for writing (or, in a dry run,
    that would be): the number ``--limit`` caps.  A transcript that is skipped,
    empty, unreadable or unindexable never consumes it.
    """

    # Consecutive "queue stayed full" outcomes after which the run gives up: the
    # queue is not draining, and each further transcript would wait out the same
    # deadline for the same answer.
    MAX_UNADMITTED: ClassVar[int] = 3

    stop: threading.Event
    ingested: int = 0
    would_ingest: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    skipped_unreadable: int = 0
    failed: int = 0
    not_attempted: int = 0
    processed: int = 0
    errors: list[str] = field(default_factory=list)
    _abort: str = field(default="", init=False)
    _unadmitted: int = field(default=0, init=False)

    def halted(self) -> bool:
        """Return whether the run must stop before its next transcript."""
        if self.stop.is_set() and not self._abort:
            self._abort = "stopped before finishing"
        return bool(self._abort)

    def abort(self, reason: str) -> None:
        """Stop the run for *reason* (the first one given is kept)."""
        self._abort = self._abort or reason

    def admitted(self) -> None:
        """Note that a transcript got past the queue."""
        self._unadmitted = 0

    def not_admitted(self) -> None:
        """Note a transcript the queue would not take; give up if they keep coming."""
        self._unadmitted += 1
        if self._unadmitted >= self.MAX_UNADMITTED:
            self.abort("ingest queue stayed full")

    def stats(self, skipped_unregistered: int) -> BackfillStats:
        """Return the run's stats, naming the stop in the errors if there was one."""
        errors = [*self.errors]
        if self._abort:
            errors.append(f"backfill stopped: {self._abort}")
        return BackfillStats(
            ingested=self.ingested,
            would_ingest=self.would_ingest,
            skipped_existing=self.skipped_existing,
            skipped_unregistered=skipped_unregistered,
            skipped_empty=self.skipped_empty,
            skipped_unreadable=self.skipped_unreadable,
            failed=self.failed,
            not_attempted=self.not_attempted,
            errors=errors,
        )
