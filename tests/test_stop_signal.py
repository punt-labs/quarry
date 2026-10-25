"""StopSignal and OsFault: a stop that remembers why, and a fault named by errno."""

from __future__ import annotations

import errno

from quarry.os_fault import OsFault
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


def test_an_os_fault_is_named_by_its_errno_and_never_quotes_a_path() -> None:
    exc = OSError(errno.EMFILE, "Too many open files", "/home/jane/secret.jsonl")

    assert OsFault.named(exc) == "EMFILE"


def test_a_fault_without_an_errno_is_named_by_its_class() -> None:
    assert OsFault.named(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")) == (
        "UnicodeDecodeError"
    )
