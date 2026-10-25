"""BackfillStats and BackfillTally: what a run counted, and when it gives up."""

from __future__ import annotations

import threading

from quarry.backfill_stats import BackfillStats, BackfillTally


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

        assert stats.summary() == "5 ingested, 3 skipped, 3 failed, 4 not attempted"

    def test_a_run_stored_nothing_only_when_it_has_errors_and_no_success(self) -> None:
        assert BackfillStats(errors=["x"]).stored_nothing
        assert not BackfillStats(ingested=1, errors=["x"]).stored_nothing
        assert not BackfillStats(would_ingest=1, errors=["x"]).stored_nothing
        assert not BackfillStats().stored_nothing


class TestBackfillTally:
    def test_it_halts_once_the_stop_event_is_set(self) -> None:
        stop = threading.Event()
        tally = BackfillTally(stop)
        assert not tally.halted()

        stop.set()

        assert tally.halted()
        assert tally.stats(0).errors == ["backfill stopped: stopped before finishing"]

    def test_the_first_reason_a_run_stopped_for_is_kept(self) -> None:
        tally = BackfillTally(threading.Event())

        tally.abort("daemon shutting down")
        tally.abort("something later")

        assert tally.stats(0).errors == ["backfill stopped: daemon shutting down"]

    def test_the_stats_name_the_stop_in_the_errors(self) -> None:
        tally = BackfillTally(threading.Event())
        tally.abort("daemon shutting down")

        assert tally.stats(0).errors == ["backfill stopped: daemon shutting down"]

    def test_a_run_gives_up_after_enough_unadmitted_transcripts_in_a_row(self) -> None:
        tally = BackfillTally(threading.Event())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        assert not tally.halted()
        tally.not_admitted()

        assert tally.halted()
        assert tally.stats(0).errors == ["backfill stopped: ingest queue stayed full"]

    def test_an_admitted_transcript_resets_the_count(self) -> None:
        tally = BackfillTally(threading.Event())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        tally.admitted()
        tally.not_admitted()

        assert not tally.halted()
