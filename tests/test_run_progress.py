"""RunProgress: what a run has counted so far, and why it stopped."""

from __future__ import annotations

from quarry.backfill_tally import BackfillTally
from quarry.run_progress import RunProgress
from quarry.stop_signal import StopSignal


def test_before_the_thread_attaches_its_tally_nothing_is_counted() -> None:
    stats = RunProgress(StopSignal()).snapshot()

    assert (stats.ingested, stats.failed, stats.errors) == (0, 0, [])


def test_a_stop_before_the_tally_exists_is_still_named() -> None:
    stop = StopSignal()
    stop.set("daemon shutting down")

    stats = RunProgress(stop).snapshot()

    assert stats.stopped == "daemon shutting down"
    assert stats.errors == ["backfill stopped: daemon shutting down"]


def test_the_snapshot_is_what_the_attached_tally_has_counted_with_the_stop() -> None:
    stop = StopSignal()
    progress = RunProgress(stop)
    tally = BackfillTally(stop)
    progress.attach(tally, 4)
    tally.stored()
    tally.fail("a.jsonl: boom")
    stop.set("task cancelled")

    stats = progress.snapshot()

    assert (stats.ingested, stats.failed, stats.skipped_unregistered) == (1, 1, 4)
    assert stats.stopped == "task cancelled"
    assert stats.errors == ["backfill stopped: task cancelled", "a.jsonl: boom"]
