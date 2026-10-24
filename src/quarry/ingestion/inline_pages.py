"""Split inline content into pages, routing an ``html`` hint to the HTML extractor."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from quarry.extractors.html_extractor import HtmlExtractor
from quarry.extractors.text_extractor import TextExtractor

if TYPE_CHECKING:
    from quarry.models import PageContent


@final
class InlinePages:
    """The pages of inline content: ``remember`` text or a web-fetch capture's HTML."""

    @staticmethod
    def of(
        content: str, document_name: str, format_hint: str, *, url_sourced: bool
    ) -> list[PageContent]:
        """Split inline *content* into pages, routing ``html`` to the HTML extractor.

        The inline path serves two callers: ``remember`` (text/markdown) and the
        web-fetch ``capture`` (raw HTML).  ``TextExtractor`` handles the text
        formats; a ``html`` hint hands the raw markup to ``HtmlExtractor`` so tags
        become Markdown sections rather than being stored verbatim.  For a web-fetch
        capture the document name IS the redacted source URL, so it doubles as the
        stored ``document_path`` — keeping the page's source location on every chunk.
        A label that merely carries an ``html`` hint (``remember``, ``learn``) is not
        a page: it records no source path, as a text note does, so nothing treats it
        as the page its name spells.
        """
        if format_hint == "html":
            return HtmlExtractor().extract_from_html(
                content, document_name, document_name if url_sourced else ""
            )
        return TextExtractor().extract_raw(
            content, document_name, format_hint=format_hint
        )
