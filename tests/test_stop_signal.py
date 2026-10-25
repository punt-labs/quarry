"""StopSignal: a stop that remembers why."""

from __future__ import annotations

from quarry.stop_signal import StopSignal


def test_a_new_signal_is_not_set_and_has_no_reason() -> None:
    signal = StopSignal()

    assert not signal.is_set()
    assert signal.reason == ""


def test_the_first_reason_wins() -> None:
    signal = StopSignal()

    signal.set("daemon shutting down")
    signal.set("task cancelled")

    assert signal.is_set()
    assert signal.reason == "daemon shutting down"
