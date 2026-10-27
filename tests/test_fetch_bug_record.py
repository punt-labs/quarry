"""Tests for quarry.fetch_bug_record: the class of a bug a caller may swallow."""

from __future__ import annotations

import logging

import pytest

from quarry.fetch_bug_record import FetchBugRecord


@pytest.mark.parametrize(
    "fault",
    [
        NameError("n"),
        AssertionError("a"),
        ImportError("i"),
        KeyError("k"),
        TypeError("t"),
        ArithmeticError("x"),
    ],
    ids=lambda fault: type(fault).__name__,
)
def test_a_fault_in_the_redactor_is_named_and_does_not_replace_the_bug(
    fault: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    def shown(_url: str) -> str:
        raise fault

    record = FetchBugRecord()
    with caplog.at_level(logging.ERROR):
        record.record(ValueError("the bug"), "https://h.example/secret", shown)
    assert record.take() == "ValueError"
    [entry] = caplog.records
    assert f"(url not shown: {type(fault).__name__})" in entry.getMessage()
    assert "h.example" not in entry.getMessage()


def test_the_first_bug_wins_and_take_forgets_it() -> None:
    record = FetchBugRecord()
    record.record(ValueError("one"), "u", lambda url: url)
    record.record(KeyError("two"), "u", lambda url: url)
    assert record.take() == "ValueError"
    assert record.take() == ""
