"""Establish the extractor's page_number behavior for non-paginated formats.

The eval design's ``JudgedUnit`` is keyed on ``(document_name, page_number)`` and
assumes non-paginated formats (code, markdown) *might* carry a null/uniform
page_number, degrading the unit to document level. These tests pin the actual
behavior so the design's degradation note rests on a fact, not a guess:

    For code and markdown the extractor assigns page_number as a 1-based
    SECTION ordinal (``sections_to_pages`` -> ``page_number = i + 1``), set
    BEFORE chunking and stable across chunking configs. It is neither null nor
    uniform, so JudgedUnit degrades only to SECTION granularity (finer than
    document level), which is exactly the stable key the design needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class TestMarkdownPageNumbers:
    def test_headings_produce_sequential_section_ordinals(self, tmp_path: Path) -> None:
        # Lazy import: quarry.extractors.text_extractor -> ingestion.text_splitter
        # -> ingestion.pipeline cycles at top-level import in isolation.
        from quarry.extractors.text_extractor import TextExtractor

        md = tmp_path / "doc.md"
        md.write_text(
            "# Intro\n\nintro body\n\n"
            "# Predicate Logic\n\npredicate body\n\n"
            "# Sets\n\nset body\n",
            encoding="utf-8",
        )

        pages = TextExtractor().extract_pages(md)
        page_numbers = [p.page_number for p in pages]

        assert page_numbers == [1, 2, 3]  # 1-based, sequential
        assert len(set(page_numbers)) == len(page_numbers)  # NOT uniform
        assert all(p.page_number is not None for p in pages)  # NOT null
        assert all(p.total_pages == 3 for p in pages)


class TestCodePageNumbers:
    def test_symbols_produce_sequential_section_ordinals(self, tmp_path: Path) -> None:
        from quarry.extractors.code_extractor import CodeExtractor

        src = tmp_path / "mod.py"
        src.write_text(
            "def first():\n    return 1\n\n\n"
            "def second():\n    return 2\n\n\n"
            "def third():\n    return 3\n",
            encoding="utf-8",
        )

        pages = CodeExtractor().extract_pages(src)
        page_numbers = [p.page_number for p in pages]

        assert page_numbers == list(range(1, len(pages) + 1))  # 1-based, sequential
        assert page_numbers[0] == 1
        # A code file yields >= 1 section; the key is never null/uniform-zero.
        assert all(p.page_number >= 1 for p in pages)
