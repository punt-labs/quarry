"""Tests for PyMuPDF text extraction with soft-wrap reflow.

These drive a real fitz document end-to-end (the extractor's boundary) rather
than mocking ``get_text``, so the ``get_text("dict")`` reflow path is exercised.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pytest

from quarry.ingestion.pdf_text_extractor import extract_text_pages
from quarry.models import PageType

_WRAPPING_BODY = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu "
    "xi omicron pi rho sigma tau upsilon phi chi psi omega "
) * 6 + "finis."


def _write_pdf(path: Path, body: str, *, page_number: str | None = None) -> None:
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 523, 700), body, fontsize=11)
        if page_number is not None:
            page.insert_text((300, 815), page_number)
        doc.save(str(path))


class TestReflowExtraction:
    def test_wrapped_paragraph_is_joined(self, tmp_path: Path) -> None:
        pdf = tmp_path / "wrapped.pdf"
        _write_pdf(pdf, _WRAPPING_BODY)

        with fitz.open(pdf) as doc:
            flat_text = str(doc[0].get_text())
        flat_lines = [ln for ln in flat_text.split("\n") if ln]
        flat_longest = max(len(ln) for ln in flat_lines)

        pages = extract_text_pages(pdf, [1], 1)
        text = pages[0].text
        reflow_longest = max(len(seg) for seg in text.split("\n"))

        # Flat extraction hard-wraps to <=88 chars/line; reflow joins the wrap.
        assert flat_longest <= 100
        assert reflow_longest > 200
        assert "upsilon phi" in text

    def test_page_number_stripped(self, tmp_path: Path) -> None:
        pdf = tmp_path / "numbered.pdf"
        _write_pdf(pdf, _WRAPPING_BODY, page_number="1")

        text = extract_text_pages(pdf, [1], 1)[0].text

        assert not any(char.isdigit() for char in text)

    def test_returns_page_content_metadata(self, tmp_path: Path) -> None:
        pdf = tmp_path / "meta.pdf"
        _write_pdf(pdf, _WRAPPING_BODY)

        pages = extract_text_pages(pdf, [1], 1, document_name="custom.pdf")

        assert len(pages) == 1
        page = pages[0]
        assert page.document_name == "custom.pdf"
        assert page.page_number == 1
        assert page.total_pages == 1
        assert page.page_type == PageType.TEXT

    def test_blank_page_yields_empty_text(self, tmp_path: Path) -> None:
        pdf = tmp_path / "blank.pdf"
        with fitz.open() as doc:
            doc.new_page()
            doc.save(str(pdf))

        pages = extract_text_pages(pdf, [1], 1)

        assert pages[0].text == ""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(fitz.FileNotFoundError):
            extract_text_pages(tmp_path / "absent.pdf", [1], 1)

    def test_dict_request_excludes_preserve_images_flag(self, tmp_path: Path) -> None:
        # Image bytes must be excluded at the PyMuPDF boundary so an image-heavy
        # page does not force the extra memory that flat get_text() avoided.
        pdf_path = tmp_path / "spy.pdf"
        pdf_path.touch()
        recorded: list[tuple[str, int | None]] = []

        def get_text(kind: str = "text", **kwargs: object) -> object:
            flags = kwargs.get("flags")
            recorded.append((kind, flags if isinstance(flags, int) else None))
            if kind == "dict":
                return {
                    "height": 842.0,
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {
                                    "bbox": (72.0, 100.0, 400.0, 112.0),
                                    "spans": [{"text": "page body text"}],
                                }
                            ],
                        }
                    ],
                }
            return "page body text"

        page = MagicMock()
        page.get_text.side_effect = get_text
        doc = MagicMock()
        doc.__enter__ = lambda self: self
        doc.__exit__ = lambda self, *args: None
        doc.__getitem__ = lambda _, idx: page

        with patch("quarry.ingestion.pdf_text_extractor.fitz.open", return_value=doc):
            extract_text_pages(pdf_path, [1], 1)

        dict_flags = [flags for kind, flags in recorded if kind == "dict"]
        assert dict_flags, "extractor never requested the dict form"
        for flags in dict_flags:
            assert flags is not None
            assert not (flags & fitz.TEXT_PRESERVE_IMAGES)
