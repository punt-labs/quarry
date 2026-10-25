"""BackfillTally: what a run counts as it goes, and when it gives up."""

from __future__ import annotations

from quarry.backfill_tally import BackfillTally
from quarry.stop_signal import StopSignal


class TestBackfillTally:
    def test_the_stop_is_the_first_error_and_the_others_follow(self) -> None:
        tally = BackfillTally(StopSignal())
        tally.fail("a.jsonl: boom")
        tally.abort("daemon shutting down")

        assert tally.stats(0).errors == [
            "backfill stopped: daemon shutting down",
            "a.jsonl: boom",
        ]

    def test_every_count_has_one_method_and_lands_in_the_stats(self) -> None:
        tally = BackfillTally(StopSignal())
        tally.stored()
        tally.would_store()
        tally.skip_existing()
        tally.skip_empty()
        tally.skip_unreadable()
        tally.fail("x: y")
        tally.leave(2)
        tally.leave_unexamined(3)
        tally.note("a note")

        stats = tally.stats(5)

        assert (stats.ingested, stats.would_ingest, stats.failed) == (1, 1, 1)
        assert (stats.skipped_existing, stats.skipped_empty) == (1, 1)
        assert (stats.skipped_unreadable, stats.skipped_unregistered) == (1, 5)
        assert (stats.not_attempted, stats.not_examined) == (2, 3)
        assert stats.errors == ["x: y", "a note"]

    def test_only_an_attempt_consumes_the_limit(self) -> None:
        tally = BackfillTally(StopSignal())

        tally.skip_existing()
        tally.skip_empty()
        tally.skip_unreadable()
        assert tally.processed == 0

        tally.stored()
        tally.would_store()
        tally.fail("x: y")
        assert tally.processed == 3

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

    def test_a_run_gives_up_after_enough_unadmitted_transcripts_in_a_row(self) -> None:
        tally = BackfillTally(StopSignal())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        assert not tally.halted()
        tally.not_admitted()

        assert tally.halted()
        assert tally.stats(0).errors == ["backfill stopped: ingest queue stayed full"]

    def test_an_answered_transcript_resets_the_count(self) -> None:
        tally = BackfillTally(StopSignal())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        tally.answered()
        tally.not_admitted()

        assert not tally.halted()

    def test_a_stored_transcript_resets_the_count(self) -> None:
        tally = BackfillTally(StopSignal())

        for _ in range(BackfillTally.MAX_UNADMITTED - 1):
            tally.not_admitted()
        tally.stored()
        tally.not_admitted()

        assert not tally.halted()
