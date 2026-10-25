"""Where a finished backfill run's stats, and how it ended, are written."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.backfill_stats import BackfillStats
    from quarry.daemon.tasks import TaskState


@final
class RunRecord:
    """The task state a backfill run reports through.

    A run that stored nothing and left something undone ends ``failed``, with
    the first error as its reason; any other ends ``completed``, the stop (if
    there was one) in its results.
    """

    __slots__ = ("_dry_run", "_state")

    _dry_run: bool
    _state: TaskState

    def __new__(cls, state: TaskState, *, dry_run: bool) -> Self:
        self = super().__new__(cls)
        self._state = state
        self._dry_run = dry_run
        return self

    def stats(self, stats: BackfillStats) -> None:
        """Record *stats* and how the run ended on the task state."""
        self._state.results = stats.as_results(dry_run=self._dry_run)
        if stats.stored_nothing:
            self._state.status = "failed"
            self._state.error = f"backfill stored nothing: {stats.errors[0]}"
        else:
            self._state.status = "completed"
