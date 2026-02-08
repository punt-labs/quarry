from __future__ import annotations

from unittest.mock import MagicMock, patch

from quarry.models import PageType
from quarry.pdf_analyzer import TEXT_THRESHOLD, analyze_pdf


def _mock_page(text: str) -> MagicMock:
    page = MagicMock()
    page.get_text.return_value = text
    return page


def _mock_doc_cm(mock_doc: MagicMock) -> MagicMock:
    """Make a mock document work as a context manager."""
    mock_doc.__enter__ = lambda self: self
    mock_doc.__exit__ = lambda self, *args: None
    return mock_doc


class TestAnalyzePdf:
    def test_text_page(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        text = "x" * TEXT_THRESHOLD
        mock_doc = MagicMock()
        mock_doc.__len__ = lambda _: 1
        mock_doc.__getitem__ = lambda _, idx: _mock_page(text)

        with patch(
            "quarry.pdf_analyzer.fitz.open", return_value=_mock_doc_cm(mock_doc)
        ):
            results = analyze_pdf(pdf_path)

        assert len(results) == 1
        assert results[0].page_type == PageType.TEXT
        assert results[0].text_length == TEXT_THRESHOLD
        assert results[0].page_number == 1

    def test_image_page(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda _: 1
        mock_doc.__getitem__ = lambda _, idx: _mock_page("short")

        with patch(
            "quarry.pdf_analyzer.fitz.open", return_value=_mock_doc_cm(mock_doc)
        ):
            results = analyze_pdf(pdf_path)

        assert len(results) == 1
        assert results[0].page_type == PageType.IMAGE

    def test_mixed_pages(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        pages = [
            _mock_page("x" * 100),  # TEXT
            _mock_page("short"),  # IMAGE
            _mock_page("y" * 200),  # TEXT
        ]
        mock_doc = MagicMock()
        mock_doc.__len__ = lambda _: 3
        mock_doc.__getitem__ = lambda _, idx: pages[idx]

        with patch(
            "quarry.pdf_analyzer.fitz.open", return_value=_mock_doc_cm(mock_doc)
        ):
            results = analyze_pdf(pdf_path)

        assert len(results) == 3
        assert results[0].page_type == PageType.TEXT
        assert results[1].page_type == PageType.IMAGE
        assert results[2].page_type == PageType.TEXT

    def test_page_numbers_1_indexed(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda _: 2
        mock_doc.__getitem__ = lambda _, idx: _mock_page("short")

        with patch(
            "quarry.pdf_analyzer.fitz.open", return_value=_mock_doc_cm(mock_doc)
        ):
            results = analyze_pdf(pdf_path)

        assert results[0].page_number == 1
        assert results[1].page_number == 2
