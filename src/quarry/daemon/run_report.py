"""A backfill run's report to its task: on time, late, and part-way."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.run_record import RunRecord
from quarry.run_progress import RunProgress

if TYPE_CHECKING:
    from quarry.backfill_stats import BackfillStats
    from quarry.daemon.tasks import TaskState
    from quarry.stop_signal import StopSignal

logger = logging.getLogger(__name__)


@final
class RunReport:
    """The :class:`~quarry.daemon.scan_report.ScanReport` of one backfill run.

    The outcome is written through the run's record, whether the scan returned
    in time or after its task gave up on it (the late one also says what it
    found); part-way, the report is whatever the run's progress has counted.
    """

    __slots__ = ("_progress", "_record")

    _progress: RunProgress
    _record: RunRecord

    def __new__(cls, state: TaskState, stop: StopSignal, *, dry_run: bool) -> Self:
        self = super().__new__(cls)
        self._record = RunRecord(state, dry_run=dry_run)
        self._progress = RunProgress(stop)
        return self

    @property
    def progress(self) -> RunProgress:
        """Return the window onto the run's counts that the scan thread fills."""
        return self._progress

    def record(self, result: BackfillStats) -> None:
        """Write the scan's stats to the task."""
        self._record.stats(result)

    def late(self, result: BackfillStats) -> None:
        """Write the stats of a scan that returned after its task gave up on it."""
        logger.warning("backfill: the scan thread returned late: %s", result.summary())
        self._record.stats(result)

    def so_far(self) -> BackfillStats:
        """Return what the run has counted so far."""
        return self._progress.snapshot()
