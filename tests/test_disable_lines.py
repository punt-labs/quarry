"""Tests for the covered-child disable note."""

from __future__ import annotations

from quarry.disable_lines import CoveredDisableNote


def test_nothing_removed_says_so_then_names_parent_command() -> None:
    assert CoveredDisableNote("/p/src", "/p", []).lines() == [
        "Nothing of its own found in /p/src",
        (
            "/p/src is still captured by the registration at /p; "
            "run 'quarry disable /p' to stop it"
        ),
    ]


def test_removed_lines_sit_between_head_and_parent_command() -> None:
    lines = CoveredDisableNote("/p/src", "/p", ["  Config file removed"]).lines()
    assert lines[0] == "Removed quarry files from /p/src"
    assert lines[1] == "  Config file removed"
    assert lines[2].endswith("run 'quarry disable /p' to stop it")
