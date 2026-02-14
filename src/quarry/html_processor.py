"""HTML processing: parse, strip boilerplate, convert to Markdown sections."""

from __future__ import annotations

import logging
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from quarry.models import PageContent, PageType
from quarry.text_processor import (
    _read_text_with_fallback,
    _sections_to_pages,
    _split_markdown,
    _split_plain,
)

logger = logging.getLogger(__name__)

SUPPORTED_HTML_EXTENSIONS = frozenset({".html", ".htm"})

# Tags whose content is boilerplate, not article knowledge.
_BOILERPLATE_TAGS = frozenset(
    {"script", "style", "nav", "header", "footer", "aside", "form", "noscript"}
)


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    """Remove boilerplate tags from the DOM in place."""
    for tag in soup.find_all(_BOILERPLATE_TAGS):
        tag.decompose()


def _extract_title(soup: BeautifulSoup) -> str:
    """Extract and return the <title> text, or empty string."""
    title_tag = soup.find("title")
    if isinstance(title_tag, Tag):
        return title_tag.get_text(strip=True)
    return ""


def _html_to_markdown(html: str) -> str:
    """Convert an HTML fragment to Markdown via markdownify."""
    md: str = markdownify(html, heading_style="ATX", bullets="-")
    return md


def _has_markdown_headings(text: str) -> bool:
    """Check whether text contains any ATX-style headings (# ...)."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") and len(stripped) > 1 and stripped[1] in " #":
            return True
    return False


def process_html_file(
    file_path: Path,
    *,
    document_name: str | None = None,
) -> list[PageContent]:
    """Parse an HTML file into Markdown sections for embedding.

    Strategy:
      1. Read with encoding fallback (UTF-8 / CP1252 / Latin-1).
      2. Parse with BeautifulSoup.
      3. Extract ``<title>`` text.
      4. Remove boilerplate tags (script, style, nav, etc.).
      5. Convert cleaned body to Markdown via markdownify.
      6. Prepend title as ``# Heading`` if the body has no headings.
      7. Split on Markdown headings, or fall back to blank-line paragraphs.

    Args:
        file_path: Path to ``.html`` or ``.htm`` file.
        document_name: Override for stored document name.  Defaults to
            ``file_path.name``.

    Returns:
        List of PageContent objects, one per section.  Empty list when the
        file contains no extractable content.

    Raises:
        ValueError: If file extension is not a supported HTML format.
    """
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_HTML_EXTENSIONS:
        msg = f"Unsupported HTML format: {suffix}"
        raise ValueError(msg)

    resolved_name = document_name or file_path.name
    logger.debug("Processing HTML: %s", resolved_name)

    html_text = _read_text_with_fallback(file_path)
    soup = BeautifulSoup(html_text, "html.parser")

    title = _extract_title(soup)
    _strip_boilerplate(soup)

    body = soup.find("body")
    content_root = body if isinstance(body, Tag) else soup
    markdown = _html_to_markdown(str(content_root))

    if title:
        markdown = f"# {title}\n\n{markdown}"

    # Choose splitting strategy based on content structure.
    if _has_markdown_headings(markdown):
        sections = _split_markdown(markdown)
    else:
        sections = _split_plain(markdown)

    if not sections:
        return []

    document_path = str(file_path.resolve())
    return _sections_to_pages(sections, resolved_name, document_path, PageType.SECTION)
