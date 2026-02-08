from __future__ import annotations

from unittest.mock import MagicMock, patch

from quarry.models import PageType
from quarry.text_extractor import extract_text_pages


def _mock_page(text: str) -> MagicMock:
    page = MagicMock()
    page.get_text.return_value = text
    return page


class TestExtractTextPages:
    def test_extracts_requested_pages(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        pages = {
            0: _mock_page("Page 1 text"),
            1: _mock_page("Page 2 text"),
            2: _mock_page("Page 3 text"),
        }
        mock_doc = MagicMock()
        mock_doc.__getitem__ = lambda _, idx: pages[idx]

        with patch("quarry.text_extractor.fitz.open", return_value=mock_doc):
            results = extract_text_pages(pdf_path, [1, 3], total_pages=3)

        assert len(results) == 2
        assert results[0].page_number == 1
        assert results[0].text == "Page 1 text"
        assert results[1].page_number == 3
        assert results[1].text == "Page 3 text"

    def test_sets_metadata(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        mock_doc = MagicMock()
        mock_doc.__getitem__ = lambda _, idx: _mock_page("content")

        with patch("quarry.text_extractor.fitz.open", return_value=mock_doc):
            results = extract_text_pages(pdf_path, [1], total_pages=10)

        assert results[0].document_name == "test.pdf"
        assert results[0].total_pages == 10
        assert results[0].page_type == PageType.TEXT

    def test_strips_whitespace(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        mock_doc = MagicMock()
        mock_doc.__getitem__ = lambda _, idx: _mock_page("  text with spaces  \n")

        with patch("quarry.text_extractor.fitz.open", return_value=mock_doc):
            results = extract_text_pages(pdf_path, [1], total_pages=1)

        assert results[0].text == "text with spaces"
