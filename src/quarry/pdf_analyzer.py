from __future__ import annotations

import logging
from pathlib import Path

import fitz

from quarry.models import PageAnalysis, PageType

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS_FOR_TEXT_PAGE = 50


def analyze_pdf(pdf_path: Path) -> list[PageAnalysis]:
    """Classify each page as TEXT or IMAGE based on extractable text content.

    Raises:
        FileNotFoundError: If pdf_path does not exist.
    """
    results: list[PageAnalysis] = []

    with fitz.open(pdf_path) as doc:
        logger.debug("Opened %s: %d pages", pdf_path.name, len(doc))
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text().strip()
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
