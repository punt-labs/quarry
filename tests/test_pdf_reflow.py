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


def _page(*blocks: dict[str, object], height: float = 0.0) -> dict[str, object]:
    page: dict[str, object] = {"width": 595.0, "blocks": list(blocks)}
    if height:
        page["height"] = height
    return page


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


class TestTableOfContentsOracle:
    """Pinned oracle mirroring architecture.pdf p2 TOC geometry.

    fitz fragments each TOC entry into three lines sharing a baseline: a title
    (left, well short of the margin), a dot-leader run (middle), and a page
    number (far right, reaching the margin). Consecutive rows sit ~12pt apart.
    Without row reassembly the prose soft-wrap heuristic concatenates the title
    and page-number fragments of adjacent rows into one garbled run. The block
    is recognised as a TOC by its dot leaders and rebuilt one line per row.

    Both the spaced ``. . .`` and solid ``....`` leader forms appear, alongside
    a separate prose block on the same page that must still join — proving the
    detector separates TOC chrome from real wrapped prose.
    """

    _INTRO = (
        "This document describes the security model in full detail across "
        "several sections and appendices for the reader to consult."
    )

    def _entry(
        self, title: str, dots: str, page: str, y: float
    ) -> list[dict[str, object]]:
        # One visual row: title (x0=87), dot leader (x0=245), page number
        # (x1=523, reaching the block margin), all sharing baseline ``y``.
        return [
            _line(title, 236.0, x0=87.0, y0=y, y1=y),
            _line(dots, 503.0, x0=245.0, y0=y, y1=y),
            _line(page, 523.0, x0=513.0, y0=y, y1=y),
        ]

    def _oracle(self) -> list[str]:
        toc_lines = [
            # Chapter heading row: title at the left margin, page number right.
            _line("10 Security", 128.0, x0=72.0, y0=10.0, y1=10.0),
            _line("11", 523.0, x0=511.0, y0=10.0, y1=10.0),
            *self._entry(
                "10.1 Bearer Token Authentication", ". . . . . . . .", "11", 22.0
            ),
            *self._entry("10.2 WebSocket Security", "................", "12", 34.0),
            *self._entry("10.3 Certificate Pinning", ". . . . . . . .", "14", 46.0),
        ]
        page = _page(
            _block(_line("Contents", 200.0)),
            _block(
                _line(
                    "This document describes the security model in full detail across",
                    523.0,
                ),
                _line(
                    "several sections and appendices for the reader to consult.",
                    400.0,
                ),
            ),
            _block(*toc_lines),
        )
        return PdfReflow.from_page_dict(page).text().split("\n\n")

    def test_chapter_heading_row_reassembled(self) -> None:
        assert "10 Security 11" in self._oracle()

    def test_spaced_leader_entry_on_its_own_line(self) -> None:
        entry = self._find("10.1 Bearer Token Authentication")
        assert entry.startswith("10.1 Bearer Token Authentication")
        assert entry.endswith("11")
        assert ". . ." in entry

    def test_solid_leader_entry_on_its_own_line(self) -> None:
        entry = self._find("10.2 WebSocket Security")
        assert entry.startswith("10.2 WebSocket Security")
        assert entry.endswith("12")
        assert "...." in entry

    def test_no_entries_concatenated_into_a_run(self) -> None:
        # The failure mode was runs like "10 Security 11 10.1 Bearer ... 11 10.2".
        for paragraph in self._oracle():
            entry_starts = sum(
                marker in paragraph for marker in ("10.1", "10.2", "10.3")
            )
            assert entry_starts <= 1, f"entries concatenated: {paragraph!r}"

    def test_prose_on_the_same_page_still_joins(self) -> None:
        assert self._INTRO in self._oracle()

    def _find(self, needle: str) -> str:
        return next(p for p in self._oracle() if needle in p)


class TestDotLeaderDetection:
    """The dot-leader detector fires on TOC runs, not on prose or decimals."""

    def _is_leader(self, text: str) -> bool:
        return ReflowLine(text=text, x0=72.0, y0=0.0, x1=523.0, y1=10.0).is_dot_leader()

    def test_spaced_leader_detected(self) -> None:
        assert self._is_leader("Introduction . . . . . . . 3")

    def test_solid_leader_detected(self) -> None:
        assert self._is_leader("Introduction ............ 3")

    def test_bare_ellipsis_not_a_leader(self) -> None:
        assert not self._is_leader("The story continues... and then ends.")

    def test_spaced_ellipsis_not_a_leader(self) -> None:
        # Three spaced dots is a stylistic ellipsis, below the four-dot threshold.
        assert not self._is_leader("Wait . . . what happened here?")

    def test_decimal_number_not_a_leader(self) -> None:
        assert not self._is_leader("The value of pi is 3.14159 exactly.")

    def test_version_string_not_a_leader(self) -> None:
        assert not self._is_leader("Released as version 1.2.3.4 last week.")


class TestMixedFontRowReassembly:
    """A TOC row whose fragments differ in ``y0`` still reflows to one line.

    Real TOCs mix font sizes: a title and its page number share a baseline but
    their bbox tops (``y0``, what fitz reports) differ by a point or two. A
    fixed global y-grid would drop the two into different bands and split the
    page number off as an orphan. Adjacency clustering keeps the row together.
    """

    def _paragraphs(self) -> list[str]:
        # Entry 10.1's page number sits 2.5pt below its title/dots baseline —
        # a larger title font. A round(y0/tolerance) grid bands 100.0 and 102.5
        # separately (33 vs 34) and would split "11" off. Adjacency keeps them.
        block = _block(
            _line(
                "10.1 Bearer Token Authentication", 236.0, x0=87.0, y0=100.0, y1=110.0
            ),
            _line(". . . . . . . .", 503.0, x0=245.0, y0=100.0, y1=108.0),
            _line("11", 523.0, x0=513.0, y0=102.5, y1=112.5),
            _line("10.2 WebSocket Security", 243.0, x0=87.0, y0=112.0, y1=122.0),
            _line("................", 503.0, x0=252.0, y0=112.0, y1=120.0),
            _line("12", 523.0, x0=513.0, y0=112.0, y1=122.0),
        )
        return PdfReflow.from_page_dict(_page(block)).text().split("\n\n")

    def test_two_entries_stay_two_lines(self) -> None:
        assert len(self._paragraphs()) == 2

    def test_page_number_not_split_from_its_entry(self) -> None:
        paragraphs = self._paragraphs()
        entry = next(p for p in paragraphs if "10.1 Bearer" in p)
        assert entry.endswith("11")
        # "11" is part of the entry line, never an orphaned paragraph of its own.
        assert "11" not in paragraphs


class TestSingleDotRunStaysProse:
    """One accidental dot run must not reclassify a prose block as a TOC.

    A prose block with a single ``......`` pause, a fill-in field, or dot-art
    has exactly one dot-leader line. Requiring ``_MIN_TOC_LEADER_LINES`` keeps
    it on the prose path, where it still soft-wrap joins and de-hyphenates.
    """

    def test_single_run_block_joins_and_dehyphenates(self) -> None:
        block = _block(
            _line(
                "Sign on the dotted line ...... then develop-",
                523.0,
                y0=100.0,
                y1=110.0,
            ),
            _line("ment continues on the next line.", 300.0, y0=112.0, y1=122.0),
        )
        paragraphs = PdfReflow.from_page_dict(_page(block)).text().split("\n\n")
        assert len(paragraphs) == 1  # prose path joined the wrapped line
        assert "development continues" in paragraphs[0]  # hyphen was stripped

    def test_block_with_one_leader_is_not_toc(self) -> None:
        block = ReflowBlock(
            lines=(
                ReflowLine(
                    text="Sign here ...... please", x0=72.0, y0=0.0, x1=523.0, y1=10.0
                ),
            )
        )
        assert not block.is_table_of_contents()

    def test_block_with_two_leaders_is_toc(self) -> None:
        block = ReflowBlock(
            lines=(
                ReflowLine(text="Intro ...... 1", x0=72.0, y0=0.0, x1=523.0, y1=10.0),
                ReflowLine(text="Setup ...... 2", x0=72.0, y0=12.0, x1=523.0, y1=22.0),
            )
        )
        assert block.is_table_of_contents()


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

    def test_justified_paragraphs_split_on_sentence_boundary(self) -> None:
        # Both lines run the full width, but the first closes a sentence and the
        # next opens a new capitalised one — a paragraph boundary, not a wrap.
        page = _page(
            _block(
                _line("The first paragraph fills the column to the margin.", 523.0),
                _line("The second paragraph also runs the full column width.", 523.0),
            )
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert len(paragraphs) == 2
        assert paragraphs[0].endswith("to the margin.")
        assert paragraphs[1].startswith("The second paragraph")

    def test_justified_wrap_without_sentence_end_still_joins(self) -> None:
        # A full-width line that does not close a sentence is a wrap, not a break.
        page = _page(
            _block(
                _line("The first line runs to the margin and keeps going with", 523.0),
                _line(
                    "more of the same sentence flowing onto the next line here.", 523.0
                ),
            )
        )
        paragraphs = PdfReflow.from_page_dict(page).text().split("\n\n")
        assert len(paragraphs) == 1

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

    def test_many_line_block_joins_into_one_paragraph(self) -> None:
        # Regression guard for the O(1)-per-pair join: a block with many
        # full-width lines still reflows into a single joined paragraph.
        lines = [
            _line(f"word{n} runs on to the block right margin edge", 523.0)
            for n in range(12)
        ]
        lines.append(_line("and a short final tail.", 200.0))
        text = PdfReflow.from_page_dict(_page(_block(*lines))).text()
        paragraphs = text.split("\n\n")
        assert len(paragraphs) == 1
        assert paragraphs[0].startswith("word0 runs on")
        assert paragraphs[0].endswith("and a short final tail.")


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

    def test_line_with_non_numeric_bbox_is_skipped(self) -> None:
        page = _page(
            _block(
                {"bbox": (72.0, "top", 300.0, 12.0), "spans": [{"text": "bad coord"}]},
                _line("kept line with numeric bbox.", 300.0),
            )
        )
        text = PdfReflow.from_page_dict(page).text()
        assert "kept line with numeric bbox." in text
        assert "bad coord" not in text

    def test_block_without_lines_key_is_skipped(self) -> None:
        page: dict[str, object] = {
            "width": 595.0,
            "blocks": [{"type": 0}, _block(_line("real content.", 300.0))],
        }
        text = PdfReflow.from_page_dict(page).text()
        assert "real content." in text

    def test_all_lines_filtered_yields_empty_block_without_raising(self) -> None:
        # Every line has a malformed bbox, so from_block_dict builds a block with
        # an empty lines tuple. __post_init__ must not raise on max()/min() over
        # empty, and from_page_dict must drop the block so the page still extracts.
        bad_block = {
            "type": 0,
            "lines": [
                {"bbox": None, "spans": [{"text": "dropped one"}]},
                {"bbox": (1.0, 2.0, 3.0, 4.0), "spans": [{"text": "   "}]},
            ],
        }
        page: dict[str, object] = {
            "width": 595.0,
            "blocks": [bad_block, _block(_line("surviving content.", 300.0))],
        }
        text = PdfReflow.from_page_dict(page).text()
        assert "surviving content." in text
        assert "dropped one" not in text

    def test_empty_block_constructed_directly_has_zero_geometry(self) -> None:
        block = ReflowBlock(lines=())
        assert block.right_margin == 0.0
        assert block.width == 0.0


class TestPhysicalPageChrome:
    """Page-number stripping keys on the physical page, not the text span."""

    def test_body_value_on_tall_page_kept(self) -> None:
        # "500" sits 76% down an 842pt page — body content, not a footer.
        page = _page(
            _block(_line("Results table header row here.", 523.0, y0=100.0, y1=120.0)),
            _block(_line("500", 300.0, y0=640.0, y1=652.0)),
            _block(
                _line("Notes after the table continue here.", 523.0, y0=670.0, y1=690.0)
            ),
            height=842.0,
        )
        assert "500" in PdfReflow.from_page_dict(page).text().split("\n\n")

    def test_numeric_adjacent_to_content_in_margin_kept(self) -> None:
        # "500" is inside the bottom band but a content block sits right above it.
        page = _page(
            _block(
                _line("Body text near the page bottom edge.", 523.0, y0=780.0, y1=792.0)
            ),
            _block(_line("500", 300.0, y0=795.0, y1=807.0)),
            height=842.0,
        )
        assert "500" in PdfReflow.from_page_dict(page).text().split("\n\n")

    def test_isolated_footer_number_stripped(self) -> None:
        # A lone number far below the body is chrome and is dropped.
        page = _page(
            _block(
                _line(
                    "The body ends well above the footer here.",
                    523.0,
                    y0=100.0,
                    y1=400.0,
                )
            ),
            _block(_line("7", 300.0, y0=810.0, y1=822.0)),
            height=842.0,
        )
        assert "7" not in PdfReflow.from_page_dict(page).text().split("\n\n")
