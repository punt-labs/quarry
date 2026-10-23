"""Inline content and URL ingest: extract, scrub, funnel through the store."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, final

from quarry.display_name import DisplayName
from quarry.extractors.html_extractor import HtmlExtractor
from quarry.extractors.text_extractor import TextExtractor
from quarry.ingestion.chunk_store_funnel import _chunk_embed_store
from quarry.ingestion.extracted_document import ExtractedDocument
from quarry.ingestion.ingest_stats import IngestStats
from quarry.ingestion.web_fetch import WebFetcher

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.http_url import HttpUrl
    from quarry.ingestion.ingest_context import IngestContext, Progress
    from quarry.models import PageContent
    from quarry.results import IngestResult

# "auto" self-detects via TextExtractor; "html" is the one hint _extract_inline_pages
# (below) intercepts before TextExtractor ever sees it, routing raw markup through
# HtmlExtractor instead. The rest pass straight through to
# TextExtractor.extract_raw's own closed set.
type FormatHint = Literal["auto", "plain", "markdown", "latex", "html"]


@dataclass(frozen=True, slots=True)
class InlineIngest:
    """Inline content to ingest, with its format hint and optional scrub hook.

    ``content_scrubber``'s presence marks a scrubbed ingest: the free-form
    metadata every chunk carries (document name, summary) is redacted at the
    :class:`WebIngest` choke point exactly once, for every scrubbed caller
    (daemon capture/remember, stdio MCP, backfill). ``None`` (the default)
    stores content and metadata unredacted — extraction still runs (including
    HTML→Markdown) — so user-initiated remembers keep their content.
    """

    content: str
    format_hint: FormatHint = "auto"
    content_scrubber: Callable[[str], str] | None = None


@dataclass(frozen=True, slots=True)
class UrlIngest:
    """A URL to fetch, with fetch overrides, a scrub hook, and prefetch reuse.

    ``prefetched_html`` is the HTML the caller already fetched (e.g. via
    ``WebFetcher.fetch_body``) — skips the redundant network fetch when set.
    ``None`` (the default) means "fetch it here," which is every caller except
    the daemon's empty-chunk capture re-fetch.
    """

    url: HttpUrl
    document_name: str | None = None
    timeout: int = 30
    delay: float = 0.0
    content_scrubber: Callable[[str], str] | None = None
    prefetched_html: str | None = None


@final
class WebIngest:
    """Ingests inline content and fetched URLs through the DES-036 funnel."""

    @staticmethod
    def ingest_content(
        inline: InlineIngest,
        document_name: str,
        progress: Progress,
        context: IngestContext,
    ) -> IngestResult:
        """Ingest inline content: split into sections, chunk, embed, store.

        Args:
            inline: The content, format hint, and optional scrub hook.
            document_name: Name for the document.
            progress: Progress reporter.
            context: Shared database/settings/collection/memory-tag knobs.

        Returns:
            Dict with ingestion results.
        """
        # Choke point: the scrubber's presence marks a scrubbed ingest, so the
        # free-form metadata the chunker copies onto every chunk — the document
        # name and the summary — is redacted HERE, once, for every scrubbed
        # caller (daemon capture/remember, stdio MCP, backfill).  No caller can
        # forget it and no new surface can reintroduce the leak.  A plain ingest
        # (no scrubber) stores metadata byte-for-byte, unchanged.
        document_name, summary = WebIngest._scrub_metadata(
            DisplayName(document_name).stored(),
            context.summary,
            inline.content_scrubber,
        )

        progress("Processing: %s", DisplayName(document_name).redacted())

        pages = WebIngest._extract_inline_pages(
            inline.content, document_name, inline.format_hint
        )
        if inline.content_scrubber is not None:
            pages = [
                replace(page, text=inline.content_scrubber(page.text)) for page in pages
            ]
        progress("Sections: %d", len(pages))

        extracted = ExtractedDocument(
            pages=pages,
            stats=IngestStats(sections=len(pages)),
            source_format="inline",
        )
        # The overwrite-delete is fail-closed inside _chunk_embed_store: it fires
        # only once a replacement chunk set exists, so an empty extraction or an
        # extraction that chunks to nothing keeps the prior good document.
        return _chunk_embed_store(
            extracted, document_name, progress, replace(context, summary=summary)
        )

    @staticmethod
    def ingest_url(
        request: UrlIngest,
        progress: Progress,
        context: IngestContext,
    ) -> IngestResult:
        """Fetch a URL, extract text from HTML, chunk, embed, store.

        Args:
            request: The URL, fetch overrides, scrub hook, and prefetch reuse.
            progress: Progress reporter.
            context: Shared database/settings/collection/memory-tag knobs.

        Returns:
            Dict with ingestion results.

        Raises:
            FetchRefusedError: The fetch failed or was refused.
            UnsupportedContentError: The media type is not text.
            RowBudgetError: A row exceeds the budget.
            (All three are ``UnindexableContentError``.)
        """
        meta_url = WebIngest._meta_url(request)
        document_name = request.document_name or meta_url
        # Same choke point as ingest_content: a scrubbed URL ingest redacts the
        # metadata the chunker copies onto every chunk.  HttpUrl already
        # stripped userinfo/query/fragment from meta_url; this second pass
        # catches PII in an explicit document_name and in the summary, so no
        # caller has to scrub them itself.  Idempotent — a re-scrub of a redacted
        # value is a no-op.
        document_name, summary = WebIngest._scrub_metadata(
            document_name, context.summary, request.content_scrubber
        )

        if request.prefetched_html is not None:
            # The caller already paid for this fetch (e.g. the daemon's capture
            # re-fetch calls WebFetcher.fetch_body itself to decide HTML vs. text
            # routing) — fetching url again here would double the network I/O.
            html = request.prefetched_html
            progress("Reusing pre-fetched body (%d characters)", len(html))
        else:
            if request.delay > 0:
                # Sub-second jitter from the monotonic clock (non-security-critical)
                # to desync parallel fetchers without importing random.
                jitter = time.monotonic_ns() % 1_000_000_000 / 1_000_000_000
                time.sleep(request.delay + jitter)

            # str(request.url) is the log form: no path, query or fragment reaches
            # quarry.log (CWE-532).
            progress("Fetching: %s", request.url)
            html = WebFetcher(request.timeout).fetch(request.url)
            progress("Fetched %d characters", len(html))

        pages = HtmlExtractor().extract_from_html(html, document_name, meta_url)
        if request.content_scrubber is not None:
            pages = [
                replace(page, text=request.content_scrubber(page.text))
                for page in pages
            ]
        progress("Sections: %d", len(pages))

        extracted = ExtractedDocument(
            pages=pages,
            stats=IngestStats(sections=len(pages)),
            source_format=".html",
        )
        # The overwrite-delete is fail-closed inside _chunk_embed_store: it fires
        # only once a replacement chunk set exists, so an empty extraction — or an
        # extraction that chunks to nothing — keeps the prior good capture.
        return _chunk_embed_store(
            extracted, document_name, progress, replace(context, summary=summary)
        )

    @staticmethod
    def _extract_inline_pages(
        content: str, document_name: str, format_hint: str
    ) -> list[PageContent]:
        """Split inline *content* into pages, routing ``html`` to the HTML extractor.

        The inline path serves two callers: ``remember`` (text/markdown) and the
        web-fetch ``capture`` (raw HTML).  ``TextExtractor`` handles the text
        formats; a ``html`` hint hands the raw markup to ``HtmlExtractor`` so tags
        become Markdown sections rather than being stored verbatim.  For a web-fetch
        capture the document name IS the redacted source URL, so it doubles as the
        stored ``document_path`` — keeping the page's source location on every chunk.
        """
        if format_hint == "html":
            return HtmlExtractor().extract_from_html(
                content, document_name, document_name
            )
        return TextExtractor().extract_raw(
            content, document_name, format_hint=format_hint
        )

    @staticmethod
    def _meta_url(request: UrlIngest) -> str:
        """Return the stored name of the fetched URL: ``scheme://host[:port]/path``.

        Whatever the caller, the query, fragment and ``;params`` of the URL never
        become document metadata; a scrubbed ingest also runs the name through the
        PII scrubber.
        """
        name = request.url.document_name
        scrub = request.content_scrubber
        return name if scrub is None else scrub(name)

    @staticmethod
    def _scrub_metadata(
        document_name: str, summary: str, content_scrubber: Callable[[str], str] | None
    ) -> tuple[str, str]:
        """Redact *document_name* and *summary* if a scrubber is set; else pass through.

        Shared choke point for ``ingest_content`` and ``ingest_url``: a scrubber's
        presence marks a scrubbed ingest, so the free-form metadata every chunk
        carries is redacted exactly once, in one place, for every scrubbed caller.
        """
        if content_scrubber is None:
            return document_name, summary
        return content_scrubber(document_name), content_scrubber(summary)


# Module-level aliases: every existing caller (backfill.py, daemon/ingest_jobs.py,
# sitemap_ingest.py) and the package's public API spell these ``ingest_content(...)``
# and ``ingest_url(...)`` (no receiver) — binding the class's staticmethods to bare
# module names keeps that calling convention while the funnel's two entry points
# live as real, class-scoped methods above.
ingest_content = WebIngest.ingest_content
ingest_url = WebIngest.ingest_url

__all__ = ["InlineIngest", "UrlIngest", "WebIngest", "ingest_content", "ingest_url"]
