from __future__ import annotations

from unittest.mock import MagicMock, patch

from quarry.ingestion.pdf_text_extractor import extract_text_pages
from quarry.models import PageType


def _mock_page(text: str, *, flat: str | None = None) -> MagicMock:
    """Mock a page: ``get_text("dict")`` yields a block; ``get_text()`` a string.

    The keyed ``side_effect`` is deliberate — a single ``return_value`` would make
    the flat ``get_text()`` return the dict too, so the flat-fallback path would
    never be exercised (see PR #326 review B).
    """
    dict_payload = {
        "height": 842.0,
        "blocks": [
            {
                "type": 0,
                "lines": [{"bbox": (0.0, 0.0, 100.0, 12.0), "spans": [{"text": text}]}],
            }
        ],
    }

    def get_text(kind: str = "text", **_kwargs: object) -> object:
        return dict_payload if kind == "dict" else (text if flat is None else flat)

    page = MagicMock()
    page.get_text.side_effect = get_text
    return page


def _mock_doc_cm(mock_doc: MagicMock) -> MagicMock:
    """Make a mock document work as a context manager."""
    mock_doc.__enter__ = lambda self: self
    mock_doc.__exit__ = lambda self, *args: None
    return mock_doc


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

        with patch(
            "quarry.ingestion.pdf_text_extractor.fitz.open",
            return_value=_mock_doc_cm(mock_doc),
        ):
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

        with patch(
            "quarry.ingestion.pdf_text_extractor.fitz.open",
            return_value=_mock_doc_cm(mock_doc),
        ):
            results = extract_text_pages(pdf_path, [1], total_pages=10)

        assert results[0].document_name == "test.pdf"
        assert results[0].total_pages == 10
        assert results[0].page_type == PageType.TEXT

    def test_strips_whitespace(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        mock_doc = MagicMock()
        mock_doc.__getitem__ = lambda _, idx: _mock_page("  text with spaces  \n")

        with patch(
            "quarry.ingestion.pdf_text_extractor.fitz.open",
            return_value=_mock_doc_cm(mock_doc),
        ):
            results = extract_text_pages(pdf_path, [1], total_pages=1)

        assert results[0].text == "text with spaces"

    def test_flat_fallback_returns_string_when_reflow_empty(self, tmp_path):
        pdf_path = tmp_path / "test.pdf"
        pdf_path.touch()

        # Dict has only a lone footer page number -> reflow is empty. Flat text
        # is a non-empty STRING, which the fallback must return verbatim.
        empty_reflow = {
            "height": 842.0,
            "blocks": [
                {
                    "type": 0,
                    "lines": [
                        {"bbox": (280.0, 810.0, 300.0, 822.0), "spans": [{"text": "7"}]}
                    ],
                }
            ],
        }

        def get_text(kind="text", **_kwargs):
            return empty_reflow if kind == "dict" else "7\nflat body text\n"

        page = MagicMock()
        page.get_text.side_effect = get_text
        mock_doc = MagicMock()
        mock_doc.__getitem__ = lambda _, idx: page

        with patch(
            "quarry.ingestion.pdf_text_extractor.fitz.open",
            return_value=_mock_doc_cm(mock_doc),
        ):
            results = extract_text_pages(pdf_path, [1], total_pages=1)

        assert results[0].text == "7\nflat body text"
