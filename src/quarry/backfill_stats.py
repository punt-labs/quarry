"""What a backfill run counted, as the stats it returns and the line that tells them."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import final

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class BackfillStats:
    """Aggregate counts from a backfill run.

    ``ingested`` counts transcripts stored; a dry run counts ``would_ingest``
    instead and stores nothing.  ``failed`` counts transcripts that were
    attempted and not stored.  ``not_attempted`` counts the transcripts that
    still needed work when the run was stopped; ``not_examined`` counts those in
    projects the run never looked into (stopped before them, or their store
    could not be read), where what needed work cannot be told cheaply.
    ``stored_unconfirmed`` counts the transcripts whose job was past its commit
    when the run gave up waiting: the store may have landed or failed, so they are
    neither ingested nor failed, and are not recorded as stored.
    ``skipped_refused`` counts the transcripts refused for what they are (over
    the read limit, not a regular file): never empty, never failed, and noted in
    ``errors`` on every run.
    ``truncated`` counts the transcripts stored from their newest records only,
    the oldest having been dropped past the record bound.
    ``unlocked_captures`` counts the capture files written without their lock, on
    a filesystem that cannot lock.
    ``stopped`` is the reason the run ended early, or ``""``.
    """

    ingested: int = 0
    would_ingest: int = 0
    skipped_existing: int = 0
    skipped_unregistered: int = 0
    skipped_empty: int = 0
    skipped_unreadable: int = 0
    skipped_refused: int = 0
    truncated: int = 0
    unlocked_captures: int = 0
    failed: int = 0
    stored_unconfirmed: int = 0
    not_attempted: int = 0
    not_examined: int = 0
    stopped: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def stored_nothing(self) -> bool:
        """Return whether nothing was stored and something failed or was left.

        Whenever it is true ``errors`` is not empty: a failure, the stop, or the
        project that could not be read each leave a line.
        """
        return not self._stored and self._left > 0

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
            "skipped_refused": self.skipped_refused,
            "truncated": self.truncated,
            "unlocked_captures": self.unlocked_captures,
            "failed": self.failed,
            "stored_unconfirmed": self.stored_unconfirmed,
            "not_attempted": self.not_attempted,
            "not_examined": self.not_examined,
            "stopped": bool(self.stopped),
            "stop_reason": self.stopped,
            "errors": list(self.errors),
            "dry_run": dry_run,
        }

    def summary(self) -> str:
        """Return ``N ingested, M skipped, K failed, R not attempted`` and the rest."""
        return (
            f"{self._stored} ingested, {self._skipped} skipped, "
            f"{self.failed} failed, {self.not_attempted} not attempted"
            f"{self._notes()}"
        )

    def log(self, *, dry_run: bool) -> None:
        """Log the summary: always one line, a WARNING if the run fell short."""
        label = "backfill (dry run)" if dry_run else "backfill"
        level = logging.WARNING if self._fell_short else logging.INFO
        logger.log(level, "%s: %s", label, self.summary())

    @property
    def _stored(self) -> int:
        """Return the transcripts stored, or that a dry run would have stored."""
        return self.ingested or self.would_ingest

    @property
    def _skipped(self) -> int:
        """Return the transcripts skipped for any reason a run skips one."""
        return (
            self.skipped_existing
            + self.skipped_empty
            + self.skipped_unreadable
            + self.skipped_refused
        )

    @property
    def _left(self) -> int:
        """Return the transcripts that failed or were left undone."""
        return (
            self.failed
            + self.stored_unconfirmed
            + self.not_attempted
            + self.not_examined
        )

    @property
    def _fell_short(self) -> bool:
        """Return whether anything failed, was left, or stopped the run."""
        return bool(self._left or self.stopped)

    def _notes(self) -> str:
        """Return the summary's notes on what was unconfirmed, refused or unexamined."""
        return "".join(
            [
                f", {self.stored_unconfirmed} stored unconfirmed"
                if self.stored_unconfirmed
                else "",
                f", {self.skipped_refused} refused" if self.skipped_refused else "",
                f", {self.truncated} truncated" if self.truncated else "",
                f", {self.unlocked_captures} captures written unlocked"
                if self.unlocked_captures
                else "",
                f", {self.not_examined} not examined" if self.not_examined else "",
                f"; stopped: {self.stopped}" if self.stopped else "",
            ]
        )
