"""Tests for quarry.shown_value: one value, a quoted line and a safe argument."""

from __future__ import annotations

import shlex

import pytest

from quarry.shown_value import Shown


def test_a_plain_value_is_quoted_for_prose_and_bare_for_a_command() -> None:
    shown = Shown.of("/repo/proj")
    assert (shown.text, shown.arg) == ("'/repo/proj'", "/repo/proj")


@pytest.mark.parametrize(
    "value", ["it's", "$(id)", "`id`", "a b;c", 'q"q', "a\nb", "a‮b", "a\x1bb"]
)
def test_the_argument_is_one_word_on_one_line(value: str) -> None:
    shown = Shown.of(value)
    assert "\n" not in shown.arg
    assert "\n" not in shown.text
    if value.isprintable():
        assert shlex.split(shown.arg) == [value]
    else:
        assert shown.arg == "'<name>'" or shown.arg == "<name>"


def test_a_path_object_is_shown_by_its_string() -> None:
    from pathlib import Path

    assert Shown.of(Path("/a/b")).arg == "/a/b"


_UNPRINTABLE = ["a\u2028b", "a\x85b", "a\x1bb[2J", "a\u200bb", "a\U000e0041b"]


@pytest.mark.parametrize("value", _UNPRINTABLE)
def test_an_unprintable_value_is_never_emitted_raw_in_the_argument(value: str) -> None:
    shown = Shown.of(value, fallback=".")
    assert shown.arg == "."
    assert value not in shown.arg
    assert value not in shown.text
