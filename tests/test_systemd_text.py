"""SystemdText: unit and env-file text, read the way systemd reads it."""

from __future__ import annotations

import pytest

from quarry.systemd_text import SystemdText


def test_the_last_stop_timeout_in_the_service_section_wins() -> None:
    text = "[Service]\nTimeoutStopSec=5\nTimeoutSec=9\n"

    assert SystemdText.stop_timeout(text) == "9"


def test_other_sections_do_not_count() -> None:
    assert SystemdText.stop_timeout("[Unit]\nTimeoutStopSec=5\n") == ""


def test_an_empty_assignment_resets() -> None:
    assert (
        SystemdText.stop_timeout("[Service]\nTimeoutStopSec=5\nTimeoutStopSec=\n") == ""
    )


def test_a_backslash_joins_the_next_line() -> None:
    assert (
        SystemdText.stop_timeout("[Service]\nTimeoutStopSec=1min \\\n30s\n")
        == "1min  30s"
    )


def test_comments_are_skipped_and_only_a_newline_ends_a_line() -> None:
    text = "[Service]\n# TimeoutStopSec=1\n;TimeoutStopSec=2\nTimeoutStopSec=3\x0c4\n"

    assert SystemdText.stop_timeout(text) == "3\x0c4"


def test_the_last_env_assignment_is_returned_unquoted() -> None:
    text = 'A="1"\nB=2\nA="3"\n'

    assert SystemdText.assignment(text, "A") == "3"
    assert SystemdText.assignment(text, "MISSING") is None


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90", 90.0), ("1min 30s", 90.0), ("2h", 7200.0), ("1.5s", 1.5), ("250ms", 0.25)],
)
def test_time_spans_are_summed(text: str, seconds: float) -> None:
    assert SystemdText.timespan(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "   ", "soon", "5 parsecs", "1s junk", "9" * 30])
def test_anything_that_is_not_a_time_span_raises(text: str) -> None:
    with pytest.raises(ValueError, match="time span"):
        SystemdText.timespan(text)
