"""Pure text-splitting utilities consumed by multiple extractor classes."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)

MD_HEADER = re.compile(r"^(?=#+\s)", re.MULTILINE)
LATEX_SECTION = re.compile(r"(?=\\(?:sub)?section\{)")
BLANK_LINE_SPLIT = re.compile(r"\n\s*\n")


def read_text_with_fallback(file_path: Path) -> str:
    """Read a text file, trying UTF-8 then CP1252 then Latin-1.

    CP1252 (Windows-1252) is a superset of Latin-1 that correctly
    decodes smart quotes and other characters in the 0x80-0x9F range.
    Latin-1 is the final fallback — a 1:1 byte mapping that decodes
    any byte sequence but maps 0x80-0x9F to C1 control characters.
    """
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.info(
            "UTF-8 decode failed for %s, trying cp1252",
            file_path.name,
        )
    try:
        return file_path.read_text(encoding="cp1252")
    except UnicodeDecodeError:
        logger.info(
            "CP1252 decode failed for %s, falling back to latin-1",
            file_path.name,
        )
        return file_path.read_text(encoding="latin-1")


def split_markdown(text: str) -> list[str]:
    """Split markdown on heading lines (any level)."""
    parts = MD_HEADER.split(text)
    return [p for p in parts if p.strip()]


def split_latex(text: str) -> list[str]:
    """Split LaTeX on \\section{} or \\subsection{} commands."""
    parts = LATEX_SECTION.split(text)
    return [p for p in parts if p.strip()]


def split_plain(text: str) -> list[str]:
    """Split plain text on blank lines (paragraph boundaries)."""
    parts = BLANK_LINE_SPLIT.split(text)
    return [p for p in parts if p.strip()]


def sections_to_pages(
    sections: list[str],
    document_name: str,
    document_path: str,
    page_type: PageType,
) -> list[PageContent]:
    """Convert section strings to PageContent objects."""
    total = len(sections)
    return [
        PageContent(
            document_name=document_name,
            document_path=document_path,
            page_number=i + 1,
            total_pages=total,
            text=section,
            page_type=page_type,
        )
        for i, section in enumerate(sections)
    ]
