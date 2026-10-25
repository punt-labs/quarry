"""BackfillStats and BackfillTally: what a run counted, and when it gives up."""

from __future__ import annotations

from quarry.backfill_report import BackfillReport
from quarry.backfill_stats import BackfillStats, BackfillTally
from quarry.stop_signal import StopSignal


class TestBackfillStats:
    def test_a_real_run_reports_ingested_and_every_count(self) -> None:
        stats = BackfillStats(
            ingested=3, skipped_existing=1, skipped_unreadable=2, failed=1, errors=["a"]
        )

        assert stats.as_results(dry_run=False) == {
            "ingested": 3,
            "skipped_existing": 1,
            "skipped_unregistered": 0,
            "skipped_empty": 0,
            "skipped_unreadable": 2,
            "failed": 1,
            "not_attempted": 0,
            "not_examined": 0,
            "stopped": False,
            "stop_reason": "",
            "errors": ["a"],
            "dry_run": False,
        }

    def test_a_dry_run_reports_would_ingest_and_never_ingested(self) -> None:
        results = BackfillStats(would_ingest=4).as_results(dry_run=True)

        assert results["would_ingest"] == 4
        assert "ingested" not in results

    def test_the_summary_names_each_outcome(self) -> None:
        stats = BackfillStats(
            ingested=5, skipped_existing=2, skipped_empty=1, failed=3, not_attempted=4
        )

        assert (
            BackfillReport(stats).summary()
            == "5 ingested, 3 skipped, 3 failed, 4 not attempted"
        )

    def test_a_run_stored_nothing_only_when_something_was_left_and_none_stored(
        self,
    ) -> None:
        assert BackfillStats(failed=1).stored_nothing
        assert BackfillStats(not_attempted=1).stored_nothing
        assert BackfillStats(not_examined=1).stored_nothing
        assert not BackfillStats(ingested=1, failed=1).stored_nothing
        assert not BackfillStats(would_ingest=1, failed=1).stored_nothing
        assert not BackfillStats(errors=["a cache note"]).stored_nothing
        assert not BackfillStats().stored_nothing

    def test_the_summary_names_what_was_not_examined_and_why_it_stopped(self) -> None:
        stats = BackfillStats(
            ingested=1, not_examined=3, stopped="daemon shutting down"
        )

        assert BackfillReport(stats).summary() == (
            "1 ingested, 0 skipped, 0 failed, 0 not attempted, 3 not examined; "
            "stopped: daemon shutting down"
        )

    def test_a_stopped_run_is_visible_in_the_results_even_when_it_stored(self) -> None:
        results = BackfillStats(ingested=2, stopped="task cancelled").as_results(
            dry_run=False
        )

        assert results["stopped"] is True
        assert results["stop_reason"] == "task cancelled"
        assert BackfillStats().as_results(dry_run=False)["stopped"] is False

    def test_the_stop_is_the_first_error_and_the_others_follow(self) -> None:
        tally = BackfillTally(StopSignal())
        tally.errors.append("a.jsonl: boom")
        tally.abort("daemon shutting down")

        assert tally.stats(0).errors == [
            "backfill stopped: daemon shutting down",
            "a.jsonl: boom",
        ]


class TestBackfillTally:
    def test_it_halts_once_the_stop_event_is_set(self) -> None:
        stop = StopSignal()
        tally = BackfillTally(stop)
        assert not tally.halted()

        stop.set("task cancelled")

        assert tally.halted()
        assert tally.stats(0).errors == ["backfill stopped: task cancelled"]

    def test_the_first_reason_a_run_stopped_for_is_kept(self) -> None:
        tally = BackfillTally(StopSignal())

        tally.abort("daemon shutting down")
        tally.abort("something later")

        assert tally.stats(0).errors == ["backfill stopped: daemon shutting down"]

    def test_the_stats_name_the_stop_in_the_errors(self) -> None:
        tally = BackfillTally(StopSignal())
        tally.abort("daemon shutting down")

        assert tally.stats(0).errors == ["backfill stopped: daemon shutting down"]

    def test_a_run_gives_up_after_enough_unadmitted_transcripts_in_a_row(self) -> None:
        tally = BackfillTally(StopSignal())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        assert not tally.halted()
        tally.not_admitted()

        assert tally.halted()
        assert tally.stats(0).errors == ["backfill stopped: ingest queue stayed full"]

    def test_an_admitted_transcript_resets_the_count(self) -> None:
        tally = BackfillTally(StopSignal())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        tally.admitted()
        tally.not_admitted()

        assert not tally.halted()
