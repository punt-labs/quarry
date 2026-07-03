"""Unit tests for PDF soft-wrap reflow (quarry.ingestion.pdf_reflow)."""

from __future__ import annotations

from dataclasses import dataclass

from quarry.ingestion.pdf_reflow import PdfReflow, ReflowBlock, ReflowLine


@dataclass(frozen=True, slots=True)
class _StubPage:
    """Minimal fitz-page stand-in: ``get_text("dict")`` vs flat ``get_text()``."""

    dict_result: dict[str, object]
    flat_result: str

    def get_text(self, kind: str = "text") -> object:
        return self.dict_result if kind == "dict" else self.flat_result


def _line(
    text: str, x1: float, x0: float = 72.0, y0: float = 0.0, y1: float = 0.0
) -> dict[str, object]:
    """Build a fitz-shaped line dict with an explicit bounding box."""
    return {"bbox": (x0, y0, x1, y1), "spans": [{"text": text}]}


def _block(*lines: dict[str, object]) -> dict[str, object]:
    return {"type": 0, "lines": list(lines)}


def _page(*blocks: dict[str, object]) -> dict[str, object]:
    return {"width": 595.0, "blocks": list(blocks)}


class TestDecoratedIdentifiersGoldenOracle:
    """Pinned oracle: decorated_identifiers.pdf p1 mixed-content page.

    A heading, a two-line prose paragraph that MUST join, a StateOp block whose
    schema lines MUST stay separate, and a trailing page number that MUST strip.
    The bbox structure mirrors what ``get_text("dict")`` yields for that page.
    """

    _PROSE = (
        "State operations relate before-state and after-state variables. "
        "A prime (') marks the after-state; a query marks an input and a "
        "bang marks an output."
    )

    def _oracle_text(self) -> str:
        page = _page(
            _block(_line("Decorated Identifiers", 260.0)),
            _block(
                _line(
                    "State operations relate before-state and after-state "
                    "variables. A prime (') ",
                    523.0,
                ),
                _line(
                    "marks the after-state; a query marks an input and a "
                    "bang marks an output.",
                    300.0,
                ),
            ),
            _block(_line("count-prime :", 150.0)),
            _block(_line("in? :", 110.0)),
            _block(_line("out! :", 115.0)),
            _block(_line("count-prime = count + in?", 230.0)),
            _block(_line("out! = count", 180.0)),
            _block(_line("1", 306.0)),
        )
        return PdfReflow.from_page_dict(page).text()

    def test_prose_lines_join_into_one_paragraph(self) -> None:
        paragraphs = self._oracle_text().split("\n\n")
        assert self._PROSE in paragraphs

    def test_mid_line_compound_hyphens_preserved(self) -> None:
        text = self._oracle_text()
        assert "before-state" in text
        assert "after-state" in text

    def test_schema_lines_stay_separate(self) -> None:
        paragraphs = self._oracle_text().split("\n\n")
        for schema in (
            "count-prime :",
            "in? :",
            "out! :",
            "count-prime = count + in?",
            "out! = count",
        ):
            assert schema in paragraphs

    def test_schema_not_merged_into_prose(self) -> None:
        assert "count-prime" not in self._PROSE
        prose_paragraph = next(
            p for p in self._oracle_text().split("\n\n") if p == self._PROSE
        )
        assert "count-prime" not in prose_paragraph

    def test_trailing_page_number_stripped(self) -> None:
        paragraphs = self._oracle_text().split("\n\n")
        assert "1" not in paragraphs
        assert not self._oracle_text().rstrip().endswith("\n1")

    def test_heading_is_its_own_paragraph(self) -> None:
        assert "Decorated Identifiers" in self._oracle_text().split("\n\n")


class TestWrapJoining:
    def test_justified_lines_all_join(self) -> None:
        page = _page(
            _block(
                _line("The quick brown fox jumps over the lazy dog and the", 523.0),
                _line("cat sat quietly on the warm mat beside the window all", 523.0),
                _line("afternoon.", 180.0),
            )
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert len(paragraphs) == 1
        assert "\n" not in paragraphs[0]

    def test_ragged_right_continuation_joins(self) -> None:
        # No line reaches a shared margin, but none is a paragraph end, so the
        # ragged lines still join into one continuous paragraph.
        page = _page(
            _block(
                _line("The quick brown fox jumps over", 520.0),
                _line("the lazy dog and then it runs", 505.0),
                _line("away.", 150.0),
            )
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert paragraphs == [
            "The quick brown fox jumps over the lazy dog and then it runs away."
        ]

    def test_paragraph_break_inside_one_block(self) -> None:
        # A short sentence-ending line followed by a capitalised line is a real
        # paragraph break even though fitz grouped both paragraphs in one block.
        page = _page(
            _block(
                _line("Quarry ingests documents and serves semantic search to", 523.0),
                _line("agents through a menu bar app.", 274.0),
                _line("The system follows a single-core projection strategy", 523.0),
                _line("with thin adapter layers.", 202.0),
            )
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert len(paragraphs) == 2
        assert paragraphs[0].endswith("menu bar app.")
        assert paragraphs[1].startswith("The system follows")

    def test_short_final_line_kept_as_break_between_blocks(self) -> None:
        page = _page(
            _block(
                _line("This is the first paragraph which is fairly long and", 523.0),
                _line("wraps down to a short final line.", 240.0),
            ),
            _block(_line("A completely separate second paragraph.", 350.0)),
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert len(paragraphs) == 2
        assert paragraphs[0].endswith("short final line.")
        assert paragraphs[1] == "A completely separate second paragraph."

    def test_numeric_range_joins_without_space(self) -> None:
        # A range split at a line break must not gain a space: "10-" + "20".
        page = _page(
            _block(
                _line("The valid range is 10-", 523.0),
                _line("20 units.", 200.0),
            )
        )
        assert "10-20 units." in PdfReflow.from_page_dict(page).text()


class TestDehyphenation:
    def _joined(self, first: str, first_x1: float, second: str) -> str:
        page = _page(_block(_line(first, first_x1), _line(second, 200.0)))
        return PdfReflow.from_page_dict(page).text()

    def test_strip_wrap_hyphen_by_default(self) -> None:
        # A plain line-break hyphen is wrap hyphenation: strip and join so the
        # token matches "information" / "development" in search.
        assert self._joined("the informa-", 523.0, "tion system.") == (
            "the information system."
        )
        assert self._joined("under develop-", 523.0, "ment now.") == (
            "under development now."
        )

    def test_strip_clear_fragment(self) -> None:
        assert self._joined("It was inas-", 523.0, "much a fragment.") == (
            "It was inasmuch a fragment."
        )

    def test_keep_compound_prefix(self) -> None:
        # self- and well- are genuine compound prefixes: keep the hyphen.
        assert self._joined("a self-", 523.0, "aware agent.") == ("a self-aware agent.")
        assert self._joined("It is a well-", 523.0, "known result.") == (
            "It is a well-known result."
        )

    def test_keep_prefix_co(self) -> None:
        # co- is a listed prefix, so co-operate keeps its hyphen.
        assert self._joined("They will co-", 523.0, "operate soon.") == (
            "They will co-operate soon."
        )


class TestQuoteTerminatedBreak:
    def test_quote_closed_sentence_breaks_paragraph(self) -> None:
        # A line ending in ." is sentence-terminal; the closing quote must not
        # suppress the paragraph break before a capitalised line.
        page = _page(
            _block(
                _line('The manual says "run it."', 300.0),
                _line("The next step is entirely separate from that.", 523.0),
            )
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert len(paragraphs) == 2
        assert paragraphs[0].endswith('"run it."')
        assert paragraphs[1].startswith("The next step")


class TestPageNumberStripping:
    def _is_page_number(self, token: str) -> bool:
        line = ReflowLine(text=token, x0=72.0, y0=0.0, x1=90.0, y1=10.0)
        return ReflowBlock(lines=(line,)).is_page_number()

    def test_short_digit_runs_stripped(self) -> None:
        assert self._is_page_number("1")
        assert self._is_page_number("42")
        assert self._is_page_number("007")

    def test_four_digit_non_years_stripped(self) -> None:
        assert self._is_page_number("0999")
        assert self._is_page_number("3000")
        assert self._is_page_number("9999")

    def test_plausible_years_exempted(self) -> None:
        assert not self._is_page_number("1000")
        assert not self._is_page_number("2024")
        assert not self._is_page_number("2999")

    def test_non_numeric_kept(self) -> None:
        assert not self._is_page_number("12a")
        assert not self._is_page_number("Chapter")
        assert not self._is_page_number("12345")

    def test_multi_line_block_never_a_page_number(self) -> None:
        block = ReflowBlock(
            lines=(
                ReflowLine(text="1", x0=72.0, y0=0.0, x1=90.0, y1=10.0),
                ReflowLine(text="2", x0=72.0, y0=12.0, x1=90.0, y1=22.0),
            )
        )
        assert not block.is_page_number()


class TestPageNumberPositionGate:
    """A lone numeric block is stripped only in the top/bottom margin band."""

    _BODY = _block(
        _line(
            "Some prose that fills the page body across a line.",
            523.0,
            y0=100.0,
            y1=600.0,
        )
    )

    def _paragraphs(self, *blocks: dict[str, object]) -> list[str]:
        return PdfReflow.from_page_dict(_page(*blocks)).text().split("\n\n")

    def test_margin_page_number_stripped(self) -> None:
        footer = _block(_line("42", 300.0, y0=760.0, y1=772.0))
        assert "42" not in self._paragraphs(self._BODY, footer)

    def test_body_numeric_block_kept(self) -> None:
        cell = _block(_line("500", 300.0, y0=340.0, y1=352.0))
        footer = _block(_line("7", 300.0, y0=760.0, y1=772.0))
        paragraphs = self._paragraphs(self._BODY, cell, footer)
        assert "500" in paragraphs  # a numeric table cell in the body is content
        assert "7" not in paragraphs  # the footer page number is still stripped


class TestFlatTextFallback:
    """A reflow that yields nothing must not silently drop an extractable page."""

    def test_falls_back_to_flat_when_reflow_empty(self) -> None:
        # A page whose only block is a lone margin page number reflows to "".
        page = _StubPage(_page(_block(_line("7", 300.0))), "7\n")
        assert PdfReflow.page_text(page) == "7\n"

    def test_prefers_reflow_when_non_empty(self) -> None:
        page = _StubPage(_page(_block(_line("Hello world.", 300.0))), "IGNORED")
        assert PdfReflow.page_text(page) == "Hello world."

    def test_empty_reflow_and_empty_flat_returns_empty(self) -> None:
        page = _StubPage(_page(), "   \n")
        assert PdfReflow.page_text(page).strip() == ""


class TestMalformedInput:
    """A broken line or block is skipped, not fatal to the whole document."""

    def test_line_without_bbox_is_skipped(self) -> None:
        page = _page(
            _block(
                {"spans": [{"text": "orphan line with no bbox"}]},
                _line("kept line with a bbox.", 300.0),
            )
        )
        text = PdfReflow.from_page_dict(page).text()
        assert "kept line with a bbox." in text
        assert "orphan" not in text

    def test_block_without_lines_key_is_skipped(self) -> None:
        page: dict[str, object] = {
            "width": 595.0,
            "blocks": [{"type": 0}, _block(_line("real content.", 300.0))],
        }
        text = PdfReflow.from_page_dict(page).text()
        assert "real content." in text
