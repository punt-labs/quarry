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


def test_an_assignment_value_loses_one_matching_pair_of_either_quote() -> None:
    text = "A='1'\nB=\"2\"\nC='3\"\n"

    assert SystemdText.assignment(text, "A") == "1"
    assert SystemdText.assignment(text, "B") == "2"
    assert SystemdText.assignment(text, "C") == "'3\""


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1w", 604800.0),
        ("2weeks", 1209600.0),
        ("1week", 604800.0),
        ("1M", 2629800.0),
        ("1month", 2629800.0),
        ("2months", 5259600.0),
        ("1y", 31557600.0),
        ("1year", 31557600.0),
        ("2years", 63115200.0),
        ("1m", 60.0),
    ],
)
def test_long_units_carry_systemds_values(text: str, seconds: float) -> None:
    assert SystemdText.timespan(text) == pytest.approx(seconds)


def test_a_month_and_a_minute_are_told_apart_by_case() -> None:
    assert SystemdText.timespan("1M") != SystemdText.timespan("1m")


@pytest.mark.parametrize("text", ["1MIN", "1S", "1H", "1Week", "1MS"])
def test_unit_names_are_case_sensitive_as_in_systemd(text: str) -> None:
    with pytest.raises(ValueError, match="time span"):
        SystemdText.timespan(text)
