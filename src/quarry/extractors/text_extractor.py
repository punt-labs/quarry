"""Text format extraction: plain text, Markdown, LaTeX, DOCX."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Self

from quarry.ingestion.text_splitter import (
    LATEX_SECTION,
    MD_HEADER,
    read_text_with_fallback,
    sections_to_pages,
    split_latex,
    split_markdown,
    split_plain,
)
from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)

_TEXT_FORMATS: dict[str, str] = {
    ".txt": "plain",
    ".md": "markdown",
    ".tex": "latex",
    ".docx": "docx",
}

SUPPORTED_TEXT_EXTENSIONS = frozenset(_TEXT_FORMATS)


class TextExtractor:
    """Extract pages from text-based document files.

    Implements ``FormatExtractor`` protocol.  Dispatches to format-specific
    splitting based on file extension: plain text on blank lines, Markdown
    on headings, LaTeX on section commands, DOCX on heading styles.
    """

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Extract logical pages from a text file."""
        suffix = path.suffix.lower()
        fmt = _TEXT_FORMATS.get(suffix)
        if fmt is None:
            msg = f"Unsupported text format: {suffix}"
            raise ValueError(msg)

        resolved_name = document_name or path.name
        logger.debug("Processing %s as %s", resolved_name, fmt)

        if fmt == "docx":
            return self._extract_docx(path, document_name=resolved_name)

        text = read_text_with_fallback(path)
        return self._split_by_format(text, fmt, resolved_name, str(path.resolve()))

    def extract_raw(
        self,
        text: str,
        document_name: str,
        format_hint: str = "auto",
    ) -> list[PageContent]:
        """Process raw text string into sections.

        Args:
            text: Raw text content.
            document_name: Name for the document.
            format_hint: One of 'auto', 'plain', 'markdown', 'latex'.

        Returns:
            List of PageContent objects, one per section.
        """
        if format_hint == "auto":
            format_hint = self._detect_format(text)
            logger.debug("Auto-detected format: %s", format_hint)

        return self._split_by_format(text, format_hint, document_name, "")

    @staticmethod
    def _detect_format(text: str) -> str:
        """Detect text format from content."""
        if MD_HEADER.search(text):
            return "markdown"
        if LATEX_SECTION.search(text):
            return "latex"
        return "plain"

    @staticmethod
    def _split_by_format(
        text: str,
        fmt: str,
        document_name: str,
        document_path: str,
    ) -> list[PageContent]:
        """Split text into sections based on format."""
        if fmt == "markdown":
            sections = split_markdown(text)
        elif fmt == "latex":
            sections = split_latex(text)
        else:
            sections = split_plain(text)

        logger.debug(
            "Split %s into %d sections (%s)", document_name, len(sections), fmt
        )
        return sections_to_pages(
            sections, document_name, document_path, PageType.SECTION
        )

    @staticmethod
    def _extract_docx(
        file_path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Extract text from DOCX, splitting on Heading styles."""
        import docx  # noqa: PLC0415

        doc = docx.Document(str(file_path))
        sections: list[str] = []
        current: list[str] = []

        for para in doc.paragraphs:
            style = para.style
            style_name = style.name if style is not None else None
            is_heading = style_name is not None and style_name.startswith("Heading")
            if is_heading and current:
                sections.append("\n".join(current))
                current = []
            if para.text.strip():
                current.append(para.text)

        if current:
            sections.append("\n".join(current))

        resolved_name = document_name or file_path.name
        document_path = str(file_path.resolve())
        return sections_to_pages(
            sections, resolved_name, document_path, PageType.SECTION
        )
