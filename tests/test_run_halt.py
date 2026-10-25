"""RunHalt: when a run stops, its first reason, and the full-queue streak."""

from __future__ import annotations

from quarry.admission_streak import AdmissionStreak
from quarry.backfill_writer import WriteOutcome
from quarry.run_halt import RunHalt
from quarry.stop_signal import StopSignal


def test_a_run_is_not_halted_until_something_stops_it() -> None:
    halt = RunHalt(StopSignal())

    assert not halt.halted()
    assert halt.reason == ""


def test_a_stop_signal_halts_it_with_the_signals_reason() -> None:
    stop = StopSignal()
    halt = RunHalt(stop)

    stop.set("daemon shutting down")

    assert halt.halted()
    assert halt.reason == "daemon shutting down"


def test_the_first_reason_is_kept() -> None:
    stop = StopSignal()
    halt = RunHalt(stop)
    halt.abort("ingest queue stayed full")

    stop.set("task cancelled")
    halt.halted()

    assert halt.reason == "ingest queue stayed full"


def test_a_job_that_had_started_stops_the_run_with_its_reason() -> None:
    halt = RunHalt(StopSignal())

    halt.refused(WriteOutcome.ABORTED_RUNNING, "task cancelled")

    assert halt.halted()
    assert halt.reason == "task cancelled"


def test_enough_full_queue_answers_in_a_row_halt_it() -> None:
    halt = RunHalt(StopSignal())

    for _ in range(AdmissionStreak.MAX):
        halt.refused(WriteOutcome.NOT_ADMITTED, "full")

    assert halt.reason == "ingest queue stayed full"


def test_any_other_answer_ends_the_streak() -> None:
    halt = RunHalt(StopSignal())
    for _ in range(AdmissionStreak.MAX - 1):
        halt.refused(WriteOutcome.NOT_ADMITTED, "full")

    halt.refused(WriteOutcome.FAILED, "boom")
    halt.refused(WriteOutcome.NOT_ADMITTED, "full")

    assert not halt.halted()
