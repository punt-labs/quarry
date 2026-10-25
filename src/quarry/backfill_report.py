"""How a backfill run is told: one summary line, and what it says."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.backfill_stats import BackfillStats

logger = logging.getLogger(__name__)


@final
class BackfillReport:
    """The summary of a run's stats, and the log line that carries it."""

    __slots__ = ("_stats",)

    _stats: BackfillStats

    def __new__(cls, stats: BackfillStats) -> Self:
        self = super().__new__(cls)
        self._stats = stats
        return self

    def summary(self) -> str:
        """Return ``N ingested, M skipped, K failed, R not attempted`` and the rest."""
        stats = self._stats
        done = stats.ingested or stats.would_ingest
        skipped = (
            stats.skipped_existing + stats.skipped_empty + stats.skipped_unreadable
        )
        line = (
            f"{done} ingested, {skipped} skipped, "
            f"{stats.failed} failed, {stats.not_attempted} not attempted"
        )
        if stats.not_examined:
            line += f", {stats.not_examined} not examined"
        if stats.stopped:
            line += f"; stopped: {stats.stopped}"
        return line

    def log(self, *, dry_run: bool) -> None:
        """Log the summary: always one line, a WARNING if the run fell short."""
        stats = self._stats
        label = "backfill (dry run)" if dry_run else "backfill"
        short = stats.failed + stats.not_attempted + stats.not_examined
        level = logging.WARNING if short or stats.stopped else logging.INFO
        logger.log(level, "%s: %s", label, self.summary())
