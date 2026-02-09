from __future__ import annotations

import logging
from pathlib import Path

import fitz

from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)


def extract_text_pages(
    pdf_path: Path,
    page_numbers: list[int],
    total_pages: int,
    *,
    document_name: str | None = None,
) -> list[PageContent]:
    """Extract text from text-based PDF pages using PyMuPDF.

    Args:
        pdf_path: Path to the PDF file.
        page_numbers: 1-indexed page numbers to extract.
        total_pages: Total pages in the document.
        document_name: Override for stored document name.
            Defaults to ``pdf_path.name``.

    Returns:
        List of PageContent for each requested page.

    Raises:
        FileNotFoundError: If pdf_path does not exist.
    """
    doc_name = document_name or pdf_path.name
    results: list[PageContent] = []

    with fitz.open(pdf_path) as doc:
        logger.debug(
            "Extracting %d text pages from %s", len(page_numbers), pdf_path.name
        )
        for page_num in page_numbers:
            page = doc[page_num - 1]
            text = page.get_text().strip()
            logger.debug("Page %d: %d chars", page_num, len(text))
            results.append(
                PageContent(
                    document_name=doc_name,
                    document_path=str(pdf_path.resolve()),
                    page_number=page_num,
                    total_pages=total_pages,
                    text=text,
                    page_type=PageType.TEXT,
                )
            )

    return results
