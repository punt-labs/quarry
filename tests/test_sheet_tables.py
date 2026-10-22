"""Tests for quarry.extractors.sheet_tables: CSV parsing under the page-budget bound."""

from __future__ import annotations

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.extractors.sheet_tables import SheetTables


def test_rows_are_parsed() -> None:
    assert SheetTables.parse_csv('a,b\n1,"x,y"\n') == [["a", "b"], ["1", "x,y"]]


def test_an_empty_text_has_no_rows() -> None:
    assert SheetTables.parse_csv("") == []


def test_a_field_over_the_cap_is_a_fixed_refusal() -> None:
    with pytest.raises(UnindexableContentError) as info:
        SheetTables.parse_csv("a," + "x" * 2**20)
    assert str(info.value) == "content rejected: CSV field exceeds the page budget"
