from __future__ import annotations

from pathlib import Path

import fitz

from quarry.models import PageContent, PageType


def extract_text_pages(
    pdf_path: Path,
    page_numbers: list[int],
    total_pages: int,
) -> list[PageContent]:
    """Extract text from text-based PDF pages using PyMuPDF.

    Args:
        pdf_path: Path to the PDF file.
        page_numbers: 1-indexed page numbers to extract.
        total_pages: Total pages in the document.

    Returns:
        List of PageContent for each requested page.
    """
    doc = fitz.open(pdf_path)
    results: list[PageContent] = []

    for page_num in page_numbers:
        page = doc[page_num - 1]
        text = page.get_text().strip()
        results.append(
            PageContent(
                document_name=pdf_path.name,
                document_path=str(pdf_path.resolve()),
                page_number=page_num,
                total_pages=total_pages,
                text=text,
                page_type=PageType.TEXT,
            )
        )

    doc.close()
    return results
