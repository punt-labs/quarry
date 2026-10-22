"""Tests for quarry.ingestion.page_chunker: guard, page split, and chunk packing."""

from __future__ import annotations

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.db.row_budget import RowBudget
from quarry.ingestion.page_chunker import ChunkLabels, PageChunker
from quarry.models import PageContent, PageType

_MIB = 1024 * 1024


def _page(text: str, page_number: int = 1, total_pages: int = 1) -> PageContent:
    return PageContent(
        document_name="test.pdf",
        document_path="/tmp/test.pdf",
        page_number=page_number,
        total_pages=total_pages,
        text=text,
        page_type=PageType.TEXT,
    )


def _chunker(max_chars: int = 100, overlap_chars: int = 20) -> PageChunker:
    return PageChunker(max_chars, overlap_chars)


class TestChunking:
    def test_empty_pages(self) -> None:
        assert _chunker().chunk([_page("")]) == []

    def test_whitespace_only_pages(self) -> None:
        assert _chunker().chunk([_page("   \n\t  ")]) == []

    def test_no_pages(self) -> None:
        assert _chunker().chunk([]) == []

    def test_single_page_single_chunk(self) -> None:
        [chunk] = _chunker().chunk([_page("Short text.")])
        assert chunk.text == "Short text."
        assert chunk.page_raw_text == "Short text."
        assert chunk.chunk_index == 0
        assert chunk.page_number == 1

    def test_every_chunk_preserves_the_page_raw_text(self) -> None:
        long_text = "First. " * 50
        result = PageChunker(50, 10).chunk([_page(long_text)])
        assert len(result) > 1
        assert {c.page_raw_text for c in result} == {long_text}

    def test_chunk_indices_sequential(self) -> None:
        result = PageChunker(50, 10).chunk([_page("Sentence one. " * 30)])
        assert [c.chunk_index for c in result] == list(range(len(result)))

    def test_multiple_pages(self) -> None:
        pages = [_page("Page one text.", 1), _page("Page two text.", 2)]
        result = _chunker().chunk(pages)
        assert [c.page_number for c in result] == [1, 2]

    def test_metadata_preserved(self) -> None:
        page = PageContent(
            document_name="report.pdf",
            document_path="/data/report.pdf",
            page_number=5,
            total_pages=20,
            text="Content here.",
            page_type=PageType.IMAGE,
        )
        [chunk] = PageChunker(100, 20, ChunkLabels(source_format=".pdf")).chunk([page])
        assert (chunk.document_name, chunk.document_path) == (
            "report.pdf",
            "/data/report.pdf",
        )
        assert (chunk.page_number, chunk.total_pages) == (5, 20)
        assert chunk.page_type == "text"

    def test_timestamp_set(self) -> None:
        [chunk] = _chunker().chunk([_page("Some text.")])
        assert chunk.ingestion_timestamp is not None

    def test_page_type_mapped_from_page_content(self) -> None:
        code = PageContent(
            "main.py", "/tmp/main.py", 1, 1, "def f(): pass", PageType.CODE
        )
        assert _chunker().chunk([code])[0].page_type == "code"

    def test_labels_propagate(self) -> None:
        labels = ChunkLabels(
            collection="docs",
            source_format=".md",
            agent_handle="kpz",
            memory_type="fact",
            summary="s",
        )
        [chunk] = PageChunker(100, 20, labels).chunk([_page("Some text.")])
        assert (
            chunk.collection,
            chunk.source_format,
            chunk.agent_handle,
            chunk.memory_type,
            chunk.summary,
        ) == ("docs", ".md", "kpz", "fact", "s")

    def test_labels_default_to_empty(self) -> None:
        [chunk] = _chunker().chunk([_page("Some text.")])
        assert (chunk.collection, chunk.source_format) == ("default", "")
        assert (chunk.agent_handle, chunk.memory_type, chunk.summary) == ("", "", "")


class TestBinaryGuard:
    def test_a_pdf_stored_as_text_is_refused(self) -> None:
        pdf = "<!-- media_type: application/pdf -->\n%PDF-1.5\n" + "�" * 5000
        with pytest.raises(UnindexableContentError, match="pdf header"):
            _chunker().chunk([_page(pdf)])

    def test_dense_non_text_is_refused(self) -> None:
        with pytest.raises(UnindexableContentError, match="non-text ratio"):
            _chunker().chunk([_page("a" * 100 + "�" * 100)])

    def test_small_nul_bearing_pdf_pages_pass(self) -> None:
        pages = [_page("body text. " * 50 + "\x00" * 3, n, 40) for n in range(1, 41)]
        assert _chunker(1800, 200).chunk(pages)

    def test_a_dense_small_page_inside_a_clean_document_passes(self) -> None:
        clean = [_page("ordinary prose. " * 200, n, 41) for n in range(1, 41)]
        dense = _page("a" * 445 + "�" * 232, 41, 41)
        assert PageChunker(1800, 200).chunk([*clean, dense])


class TestPageSplit:
    def test_a_three_mib_page_is_split_to_the_row_budget(self) -> None:
        text = "word " * (3 * _MIB // 5)
        chunks = PageChunker().chunk([_page(text)])
        pages = {c.page_number: c.page_raw_text for c in chunks}
        assert len(pages) == 3
        assert all(len(p.encode()) <= _MIB for p in pages.values())

    def test_the_split_parts_reproduce_the_input(self) -> None:
        text = "A sentence that repeats. " * (3 * _MIB // 25)
        chunks = PageChunker().chunk([_page(text)])
        pages = {c.page_number: c.page_raw_text for c in chunks}
        assert "".join(pages[n] for n in sorted(pages)) == text

    def test_split_pages_are_numbered_for_the_whole_document(self) -> None:
        pages = [_page("short intro.", 1, 2), _page("word " * (2 * _MIB // 5), 2, 2)]
        chunks = PageChunker().chunk(pages)
        assert sorted({c.page_number for c in chunks}) == [1, 2, 3]
        assert {c.total_pages for c in chunks} == {3}

    def test_an_unsplit_document_keeps_its_numbering(self) -> None:
        pages = [_page("one.", 4, 9), _page("two.", 7, 9)]
        chunks = _chunker().chunk(pages)
        assert [(c.page_number, c.total_pages) for c in chunks] == [(4, 9), (7, 9)]

    def test_every_split_chunk_fits_a_row(self) -> None:
        budget = RowBudget()
        for chunk in PageChunker().chunk([_page("word " * (2 * _MIB // 5))]):
            budget.require_page(chunk.page_raw_text)
            budget.require_chunk(chunk.text)


class TestChunkBound:
    def test_an_unpunctuated_run_is_wrapped(self) -> None:
        chunks = PageChunker().chunk([_page("x" * 422_780)])
        assert len(chunks) > 1
        assert max(len(c.text) for c in chunks) <= 1800

    def test_every_chunk_is_at_most_max_chars(self) -> None:
        text = ("A long unpunctuated clause " * 40 + ". Short one. ") * 20
        chunks = PageChunker(300, 50).chunk([_page(text)])
        assert max(len(c.text) for c in chunks) <= 300


class TestConstruction:
    def test_settings_whose_chunks_could_exceed_the_chunk_budget_are_refused(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="chunk budget"):
            PageChunker(16_000, 1_000)

    def test_a_non_positive_chunk_size_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_chars"):
            PageChunker(0, 0)

    def test_the_default_settings_fit_the_budget(self) -> None:
        PageChunker(1800, 200)
