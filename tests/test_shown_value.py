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
        assert shown.arg == "'<name>'"


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


def test_a_printable_value_is_not_a_fallback() -> None:
    assert not Shown.of("/repo/proj").is_fallback


@pytest.mark.parametrize("value", _UNPRINTABLE)
def test_an_unprintable_value_is_marked_as_standing_in_for_the_fallback(
    value: str,
) -> None:
    assert Shown.of(value, fallback=".").is_fallback


def test_either_picks_the_usual_text_for_a_printable_value() -> None:
    assert Shown.of("ok").either(usual="u", instead="i") == "u"


def test_either_picks_the_instead_text_for_a_fallback() -> None:
    assert Shown.of("a\u2028b").either(usual="u", instead="i") == "i"
