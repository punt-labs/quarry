"""Tests for PyMuPDF text extraction with soft-wrap reflow.

These drive a real fitz document end-to-end (the extractor's boundary) rather
than mocking ``get_text``, so the ``get_text("dict")`` reflow path is exercised.
"""

from __future__ import annotations

from pathlib import Path

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
            page.insert_text((300, 730), page_number)
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
