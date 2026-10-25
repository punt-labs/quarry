"""RunRecord: a run's outcome reaches its task on time, late, and part-way."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.backfill_stats import BackfillStats
from quarry.backfill_tally import BackfillTally
from quarry.daemon.run_record import RunRecord
from quarry.daemon.tasks import TaskState
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    import pytest


def _record() -> tuple[RunRecord, TaskState]:
    state = TaskState(task_id="t", kind="backfill")
    return RunRecord(state, StopSignal(), dry_run=False), state


def test_the_outcome_is_written_to_the_task() -> None:
    record, state = _record()

    record.record(BackfillStats(ingested=2))

    assert (state.status, state.results["ingested"]) == ("completed", 2)


def test_a_run_that_stored_nothing_and_left_something_fails_with_the_first_error() -> (
    None
):
    record, state = _record()

    record.record(BackfillStats(failed=1, errors=["a.jsonl: unreadable"]))

    assert state.status == "failed"
    assert state.error == "backfill stored nothing: a.jsonl: unreadable"


def test_a_late_outcome_is_written_and_says_what_it_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record, state = _record()

    with caplog.at_level(logging.WARNING, logger="quarry.daemon.run_record"):
        record.late(BackfillStats(ingested=3, not_attempted=1))

    assert state.results["ingested"] == 3
    assert "3 ingested" in caplog.text
    assert "1 not attempted" in caplog.text


def test_part_way_the_record_is_what_the_run_has_counted() -> None:
    record, _ = _record()
    tally = BackfillTally(StopSignal())
    record.progress.attach(tally, 0)
    tally.stored()

    assert record.so_far().ingested == 1
