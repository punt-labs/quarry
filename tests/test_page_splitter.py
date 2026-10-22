"""Tests for quarry.ingestion.page_splitter: never truncate, always fit."""

from __future__ import annotations

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.page_splitter import PageSplitter
from quarry.models import PageContent, PageType


def _page(
    text: str,
    number: int = 1,
    total: int = 1,
    page_type: PageType = PageType.SECTION,
) -> PageContent:
    return PageContent("d.md", "/d.md", number, total, text, page_type)


class TestSplit:
    def test_a_page_within_the_budget_is_one_part(self) -> None:
        assert PageSplitter(100).split("a" * 100) == ["a" * 100]

    def test_a_page_of_exactly_the_budget_is_not_split(self) -> None:
        assert PageSplitter(1_048_576).split("a" * 1_048_576) == ["a" * 1_048_576]

    def test_a_page_one_byte_over_is_split(self) -> None:
        parts = PageSplitter(1_048_576).split("a" * 1_048_577)
        assert [len(p) for p in parts] == [1_048_576, 1]

    def test_multibyte_text_is_measured_in_bytes(self) -> None:
        parts = PageSplitter(100).split("é" * 60)  # 120 bytes
        assert len(parts) == 2
        assert all(len(p.encode()) <= 100 for p in parts)

    def test_a_cut_never_lands_inside_a_character(self) -> None:
        text = "日本語" * 100  # three bytes per character
        parts = PageSplitter(100).split(text)
        assert "".join(parts) == text
        assert all(len(p.encode()) <= 100 for p in parts)

    def test_the_parts_reproduce_the_input(self) -> None:
        text = "Para one.\n\nPara two is longer.\n\nPara three.\n" * 40
        parts = PageSplitter(200).split(text)
        assert "".join(parts) == text
        assert all(len(p.encode()) <= 200 for p in parts)

    def test_a_blank_line_is_preferred_over_a_newline(self) -> None:
        text = "x" * 60 + "\n" + "y" * 20 + "\n\n" + "z" * 100
        assert PageSplitter(100).split(text)[0] == "x" * 60 + "\n" + "y" * 20 + "\n\n"

    def test_a_newline_is_preferred_over_a_sentence_end(self) -> None:
        text = "x" * 55 + ". " + "y" * 10 + "\n" + "z" * 100
        assert PageSplitter(100).split(text)[0].endswith("\n")

    def test_a_sentence_end_is_preferred_over_a_space(self) -> None:
        text = "x" * 55 + ". " + "y" * 10 + " " + "z" * 100
        assert PageSplitter(100).split(text)[0].endswith(". ")

    def test_a_space_is_used_when_there_is_nothing_better(self) -> None:
        text = "x" * 70 + " " + "z" * 100
        assert PageSplitter(100).split(text)[0] == "x" * 70 + " "

    def test_a_run_without_boundaries_is_cut_hard(self) -> None:
        parts = PageSplitter(100).split("x" * 250)
        assert [len(p) for p in parts] == [100, 100, 50]

    def test_a_boundary_in_the_first_half_is_ignored(self) -> None:
        text = "\n\n" + "x" * 300
        assert len(PageSplitter(100).split(text)[0]) == 100

    def test_empty_text_is_one_part(self) -> None:
        assert PageSplitter(100).split("") == [""]


class TestExpand:
    def test_pages_within_budget_are_returned_unchanged(self) -> None:
        pages = [_page("one", 3, 9), _page("two", 5, 9)]
        assert PageSplitter(100).expand(pages) == pages

    def test_a_split_renumbers_the_whole_document(self) -> None:
        pages = [_page("intro", 1, 2), _page("w " * 100, 2, 2)]
        result = PageSplitter(100).expand(pages)
        assert [p.page_number for p in result] == list(range(1, len(result) + 1))
        assert {p.total_pages for p in result} == {len(result)}
        assert result[0].text == "intro"

    def test_metadata_is_kept_on_every_part(self) -> None:
        [first, *_] = PageSplitter(50).expand([_page("w " * 100)])
        assert (first.document_name, first.document_path) == ("d.md", "/d.md")
        assert first.page_type is PageType.SECTION


class TestPaginatedFormats:
    """Physical pages keep their numbers: an oversized one fails the document."""

    @pytest.mark.parametrize(
        "page_type", [PageType.TEXT, PageType.IMAGE, PageType.PRESENTATION]
    )
    def test_an_oversized_paginated_page_is_refused(self, page_type: PageType) -> None:
        pages = [_page("ok", 1, 3, page_type), _page("w " * 100, 2, 3, page_type)]
        with pytest.raises(UnindexableContentError, match="page 2 exceeds the page"):
            PageSplitter(100).expand(pages)

    def test_a_paginated_page_within_budget_keeps_its_numbering(self) -> None:
        pages = [_page("one", 4, 9, PageType.TEXT), _page("two", 7, 9, PageType.TEXT)]
        assert PageSplitter(100).expand(pages) == pages

    @pytest.mark.parametrize(
        "page_type", [PageType.SECTION, PageType.CODE, PageType.SPREADSHEET]
    )
    def test_logical_sections_are_split_and_renumbered(
        self, page_type: PageType
    ) -> None:
        result = PageSplitter(100).expand([_page("w " * 100, 5, 9, page_type)])
        assert [p.page_number for p in result] == list(range(1, len(result) + 1))


class TestConstruction:
    def test_a_budget_below_one_character_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            PageSplitter(3)
