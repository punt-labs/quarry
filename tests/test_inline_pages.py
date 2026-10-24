"""Tests for quarry.ingestion.inline_pages — how inline content becomes pages."""

from __future__ import annotations

from quarry.ingestion.inline_pages import InlinePages


class TestInlinePages:
    def test_an_html_page_keeps_the_name_as_the_source_path(self) -> None:
        pages = InlinePages.of(
            "<p>hello there</p>", "https://h/p", "html", url_sourced=True
        )
        assert {page.document_path for page in pages} == {"https://h/p"}

    def test_an_html_label_records_no_source_path(self) -> None:
        pages = InlinePages.of(
            "<p>hello there</p>", "https://h/p", "html", url_sourced=False
        )
        assert {page.document_path for page in pages} == {""}

    def test_markup_under_an_html_hint_becomes_markdown_text(self) -> None:
        pages = InlinePages.of(
            "<p>hello there</p>", "https://h/p", "html", url_sourced=True
        )
        assert "<p>" not in pages[0].text

    def test_a_text_hint_records_no_source_path(self) -> None:
        pages = InlinePages.of("a plain note", "a-note", "plain", url_sourced=True)
        assert {page.document_path for page in pages} == {""}
