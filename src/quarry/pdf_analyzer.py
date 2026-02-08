from __future__ import annotations

from pathlib import Path

import fitz

from quarry.models import PageAnalysis, PageType

TEXT_THRESHOLD = 50


def analyze_pdf(pdf_path: Path) -> list[PageAnalysis]:
    """Classify each page as TEXT or IMAGE based on extractable text content."""
    doc = fitz.open(pdf_path)
    results: list[PageAnalysis] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text().strip()
        text_length = len(text)

        page_type = PageType.TEXT if text_length >= TEXT_THRESHOLD else PageType.IMAGE

        results.append(
            PageAnalysis(
                page_number=page_num + 1,
                page_type=page_type,
                text_length=text_length,
            )
        )

    doc.close()
    return results
