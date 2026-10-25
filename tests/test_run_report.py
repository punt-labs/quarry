"""RunReport: a run's outcome reaches its task on time, late, and part-way."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.backfill_stats import BackfillStats
from quarry.backfill_tally import BackfillTally
from quarry.daemon.run_report import RunReport
from quarry.daemon.tasks import TaskState
from quarry.run_progress import RunProgress
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    import pytest


def _report() -> tuple[RunReport, TaskState, RunProgress]:
    state = TaskState(task_id="t", kind="backfill")
    report = RunReport(state, StopSignal(), dry_run=False)
    return report, state, report.progress


def test_the_outcome_is_written_to_the_task() -> None:
    report, state, _ = _report()

    report.record(BackfillStats(ingested=2))

    assert (state.status, state.results["ingested"]) == ("completed", 2)


def test_a_late_outcome_is_written_and_says_what_it_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report, state, _ = _report()

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.run_report"):
        report.late(BackfillStats(ingested=3, not_attempted=1))

    assert state.results["ingested"] == 3
    assert "3 ingested" in caplog.text
    assert "1 not attempted" in caplog.text


def test_part_way_the_report_is_what_the_run_has_counted() -> None:
    report, _, progress = _report()
    tally = BackfillTally(StopSignal())
    progress.attach(tally, 0)
    tally.stored()

    assert report.so_far().ingested == 1
