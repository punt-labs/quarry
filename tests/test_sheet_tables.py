"""Tests for quarry.extractors.sheet_tables: CSV parsing under the page-budget bound."""

from __future__ import annotations

import csv
import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_the_limit_after_import_is_the_page_cap() -> None:
    from quarry.db.row_budget import RowBudget

    assert csv.field_size_limit() == RowBudget().max_page_bytes


def test_concurrent_parses_of_large_cells_both_succeed() -> None:
    """The limit is process-global, so no parse may set and restore it."""
    text = "a\n" + "x" * 200_000 + "\n"
    start = threading.Barrier(4)

    def work() -> int:
        start.wait()
        return sum(len(SheetTables.parse_csv(text)) for _ in range(300))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(work) for _ in range(4)]
        assert [future.result() for future in futures] == [600] * 4
