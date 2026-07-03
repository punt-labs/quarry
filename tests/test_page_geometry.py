"""Unit tests for page-number chrome detection (quarry.ingestion.page_geometry)."""

from __future__ import annotations

from quarry.ingestion.page_geometry import PageChrome
from quarry.ingestion.pdf_reflow import ReflowBlock, ReflowLine


def _block(text: str, y0: float, y1: float, x1: float = 300.0) -> ReflowBlock:
    line = ReflowLine(text=text, x0=72.0, y0=y0, x1=x1, y1=y1)
    return ReflowBlock(lines=(line,))


class TestPageChrome:
    def test_isolated_footer_in_bottom_margin_is_droppable(self) -> None:
        chrome = PageChrome.for_page((), page_height=842.0)
        footer = _block("7", 810.0, 822.0)
        body = _block("Body content well above.", 100.0, 400.0, x1=523.0)
        assert chrome.is_droppable(footer, (body,))

    def test_body_numeric_outside_margin_kept(self) -> None:
        chrome = PageChrome.for_page((), page_height=842.0)
        value = _block("500", 640.0, 652.0)  # 76% down — not in the band
        body = _block("Header row.", 100.0, 120.0, x1=523.0)
        assert not chrome.is_droppable(value, (body,))

    def test_numeric_in_margin_but_adjacent_kept(self) -> None:
        chrome = PageChrome.for_page((), page_height=842.0)
        value = _block("500", 795.0, 807.0)  # in the band
        neighbour = _block("Body edge line.", 780.0, 792.0, x1=523.0)  # gap ~3pt
        assert not chrome.is_droppable(value, (neighbour,))

    def test_sole_block_in_margin_is_isolated(self) -> None:
        chrome = PageChrome.for_page((), page_height=842.0)
        footer = _block("3", 810.0, 822.0)
        assert chrome.is_droppable(footer, ())

    def test_non_page_number_never_droppable(self) -> None:
        chrome = PageChrome.for_page((), page_height=842.0)
        heading = _block("Chapter", 810.0, 822.0)
        assert not chrome.is_droppable(heading, ())

    def test_for_page_falls_back_to_text_span_without_height(self) -> None:
        footer = _block("9", 760.0, 772.0)
        body = _block("Body text.", 100.0, 600.0, x1=523.0)
        chrome = PageChrome.for_page((body, footer), page_height=0.0)
        assert chrome.is_droppable(footer, (body,))
