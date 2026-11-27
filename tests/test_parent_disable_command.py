"""Tests for the parent-disable command line the covered-child report offers."""

from __future__ import annotations

import shlex
from collections.abc import Iterator

import pytest

from quarry.db_pointer import SELECTION
from quarry.parent_disable_command import ParentDisableCommand


@pytest.fixture(autouse=True)
def _clear_override() -> Iterator[None]:
    yield
    SELECTION.override("")


def test_plain_command_without_a_database_selection() -> None:
    assert str(ParentDisableCommand("/p")) == "quarry disable /p"


def test_command_repeats_the_database_override() -> None:
    SELECTION.override("work")
    assert str(ParentDisableCommand("/p")) == "quarry --db work disable /p"


def test_path_with_spaces_and_a_quote_stays_one_argument() -> None:
    parent = "/my proj/it's"
    assert shlex.split(str(ParentDisableCommand(parent))) == [
        "quarry",
        "disable",
        parent,
    ]
