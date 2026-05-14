"""PDF format extraction: classify pages and extract text or OCR."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self

import fitz

from quarry.ingestion.pdf_text_extractor import extract_text_pages
from quarry.models import PageAnalysis, PageContent, PageType

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.types import OcrBackend

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS_FOR_TEXT_PAGE = 50


class PdfExtractor:
    """Extract pages from PDF files.

    Implements ``FormatExtractor`` protocol.  Classifies each page as
    text or image, then delegates to PyMuPDF text extraction or OCR
    accordingly.
    """

    _settings: Settings
    _ocr: OcrBackend

    def __new__(cls, settings: Settings, ocr: OcrBackend) -> Self:
        self = super().__new__(cls)
        self._settings = settings
        self._ocr = ocr
        return self

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Extract pages from a PDF file.

        Classifies each page as text or image, extracts text pages
        via PyMuPDF, and routes image pages to the OCR backend.
        """
        document_name = document_name or path.name
        analyses = self._classify_pages(path)
        total_pages = len(analyses)

        text_page_nums = [
            a.page_number for a in analyses if a.page_type == PageType.TEXT
        ]
        image_page_nums = [
            a.page_number for a in analyses if a.page_type == PageType.IMAGE
        ]

        all_pages: list[PageContent] = []

        if text_page_nums:
            all_pages.extend(
                extract_text_pages(
                    path, text_page_nums, total_pages, document_name=document_name
                )
            )

        if image_page_nums:
            all_pages.extend(
                self._ocr.ocr_document(
                    path,
                    image_page_nums,
                    total_pages,
                    document_name=document_name,
                )
            )

        all_pages.sort(key=lambda p: p.page_number)
        return all_pages

    @staticmethod
    def _classify_pages(pdf_path: Path) -> list[PageAnalysis]:
        """Classify each page as TEXT or IMAGE based on extractable text content."""
        results: list[PageAnalysis] = []

        with fitz.open(pdf_path) as doc:
            logger.debug("Opened %s: %d pages", pdf_path.name, len(doc))
            for page_num in range(len(doc)):
                page = doc[page_num]
                raw = page.get_text()
                text = str(raw).strip()
                text_length = len(text)

                page_type = (
                    PageType.TEXT
                    if text_length >= MIN_TEXT_CHARS_FOR_TEXT_PAGE
                    else PageType.IMAGE
                )
                logger.debug(
                    "Page %d: %s (%d chars, threshold=%d)",
                    page_num + 1,
                    page_type.value,
                    text_length,
                    MIN_TEXT_CHARS_FOR_TEXT_PAGE,
                )

                results.append(
                    PageAnalysis(
                        page_number=page_num + 1,
                        page_type=page_type,
                        text_length=text_length,
                    )
                )

        return results
