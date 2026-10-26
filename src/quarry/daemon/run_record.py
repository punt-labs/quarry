"""A backfill run's report to its task: on time, late, and part-way."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.run_progress import RunProgress

if TYPE_CHECKING:
    from quarry.backfill_stats import BackfillStats
    from quarry.daemon.tasks import TaskState
    from quarry.stop_signal import StopSignal

logger = logging.getLogger(__name__)


@final
class RunRecord:
    """The task state a backfill run reports through, and what the run has counted.

    A run that stored nothing and left something undone ends ``failed``, with
    the first error as its reason; any other ends ``completed``, the stop (if
    there was one) in its results.  The outcome is written the same way whether
    the scan returned in time or after its task gave up on it (the late one also
    says what it found); part-way, the report is whatever the run's progress has
    counted.
    """

    __slots__ = ("_dry_run", "_progress", "_state")

    _dry_run: bool
    _progress: RunProgress
    _state: TaskState

    def __new__(cls, state: TaskState, stop: StopSignal, *, dry_run: bool) -> Self:
        self = super().__new__(cls)
        self._state = state
        self._dry_run = dry_run
        self._progress = RunProgress(stop)
        return self

    @property
    def progress(self) -> RunProgress:
        """Return the window onto the run's counts that the scan thread fills."""
        return self._progress

    def record(self, stats: BackfillStats) -> None:
        """Record *stats* and how the run ended on the task state."""
        self._state.results = stats.as_results(dry_run=self._dry_run)
        if stats.stored_nothing:
            self._state.status = "failed"
            self._state.error = f"backfill stored nothing: {stats.errors[0]}"
        else:
            self._state.status = "completed"

    def late(self, stats: BackfillStats) -> None:
        """Record the stats of a scan that returned after its task gave up on it."""
        logger.warning("backfill: the scan thread returned late: %s", stats.summary())
        self.record(stats)

    def so_far(self) -> BackfillStats:
        """Return what the run has counted so far."""
        return self._progress.snapshot()
