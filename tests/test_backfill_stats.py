"""BackfillStats: what a run counted, and the line that tells it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.backfill_stats import BackfillStats

if TYPE_CHECKING:
    import pytest


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
            "skipped_refused": 0,
            "truncated": 0,
            "unlocked_captures": 0,
            "failed": 1,
            "stored_unconfirmed": 0,
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

        assert stats.summary() == "5 ingested, 3 skipped, 3 failed, 4 not attempted"

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

        assert stats.summary() == (
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

    def test_a_run_that_fell_short_is_logged_at_warning_in_one_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="quarry.backfill_stats"):
            BackfillStats(ingested=1, not_attempted=2).log(dry_run=False)

        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert "2 not attempted" in caplog.records[0].getMessage()

    def test_a_clean_dry_run_is_logged_at_info_and_says_it_was_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="quarry.backfill_stats"):
            BackfillStats(would_ingest=3).log(dry_run=True)

        assert [r.levelno for r in caplog.records] == [logging.INFO]
        assert caplog.records[0].getMessage().startswith("backfill (dry run): 3")
