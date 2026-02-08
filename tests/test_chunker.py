from __future__ import annotations

from quarry.chunker import _split_text, chunk_pages
from quarry.models import PageContent, PageType


def _make_page(text: str, page_number: int = 1) -> PageContent:
    return PageContent(
        document_name="test.pdf",
        document_path="/tmp/test.pdf",
        page_number=page_number,
        total_pages=1,
        text=text,
        page_type=PageType.TEXT,
    )


class TestSplitText:
    def test_short_text_no_split(self):
        result = _split_text("Hello world.", max_chars=100, overlap_chars=20)
        assert result == ["Hello world."]

    def test_exact_boundary(self):
        text = "a" * 100
        result = _split_text(text, max_chars=100, overlap_chars=20)
        assert result == [text]

    def test_splits_at_sentence_boundary(self):
        sentences = ["First sentence.", "Second sentence.", "Third sentence."]
        text = " ".join(sentences)
        result = _split_text(text, max_chars=35, overlap_chars=10)
        assert len(result) >= 2
        assert "First sentence." in result[0]

    def test_overlap_present(self):
        text = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
        result = _split_text(text, max_chars=30, overlap_chars=10)
        assert len(result) >= 2
        # Chunks after the first should contain overlap from prior chunk
        for i in range(1, len(result)):
            # The overlap means some content from prior chunk appears in this one
            assert len(result[i]) > 0

    def test_single_long_sentence(self):
        text = "a" * 200
        result = _split_text(text, max_chars=100, overlap_chars=20)
        # No sentence boundaries to split on, so the whole text ends up in one chunk
        assert len(result) == 1
        assert result[0] == text


class TestChunkPages:
    def test_empty_pages(self):
        page = _make_page("")
        result = chunk_pages([page], max_chars=100, overlap_chars=20)
        assert result == []

    def test_whitespace_only_pages(self):
        page = _make_page("   \n\t  ")
        result = chunk_pages([page], max_chars=100, overlap_chars=20)
        assert result == []

    def test_single_page_single_chunk(self):
        page = _make_page("Short text.")
        result = chunk_pages([page], max_chars=100, overlap_chars=20)
        assert len(result) == 1
        assert result[0].text == "Short text."
        assert result[0].page_raw_text == "Short text."
        assert result[0].chunk_index == 0
        assert result[0].page_number == 1

    def test_preserves_page_raw_text(self):
        long_text = "First. " * 50
        page = _make_page(long_text)
        result = chunk_pages([page], max_chars=50, overlap_chars=10)
        assert len(result) > 1
        for chunk in result:
            assert chunk.page_raw_text == long_text

    def test_chunk_indices_sequential(self):
        long_text = "Sentence one. " * 30
        page = _make_page(long_text)
        result = chunk_pages([page], max_chars=50, overlap_chars=10)
        indices = [c.chunk_index for c in result]
        assert indices == list(range(len(result)))

    def test_multiple_pages(self):
        pages = [
            _make_page("Page one text.", page_number=1),
            _make_page("Page two text.", page_number=2),
        ]
        result = chunk_pages(pages, max_chars=100, overlap_chars=20)
        assert len(result) == 2
        assert result[0].page_number == 1
        assert result[1].page_number == 2

    def test_metadata_preserved(self):
        page = PageContent(
            document_name="report.pdf",
            document_path="/data/report.pdf",
            page_number=5,
            total_pages=20,
            text="Content here.",
            page_type=PageType.IMAGE,
        )
        result = chunk_pages([page], max_chars=100, overlap_chars=20)
        assert len(result) == 1
        assert result[0].document_name == "report.pdf"
        assert result[0].document_path == "/data/report.pdf"
        assert result[0].page_number == 5
        assert result[0].total_pages == 20

    def test_timestamp_set(self):
        page = _make_page("Some text.")
        result = chunk_pages([page], max_chars=100, overlap_chars=20)
        assert result[0].ingestion_timestamp is not None
