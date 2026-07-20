"""Document ingestion pipeline: dispatch by format, chunk, embed, store."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from quarry.models import Chunk
    from quarry.sitemap import SitemapEntry

from quarry.capture_url import CaptureUrl
from quarry.config import Settings
from quarry.db import Database
from quarry.extractors.html_extractor import HtmlExtractor
from quarry.extractors.image_extractor import SUPPORTED_IMAGE_EXTENSIONS, ImageExtractor
from quarry.extractors.pdf_extractor import PdfExtractor
from quarry.extractors.text_extractor import TextExtractor
from quarry.ingestion.backends import get_ocr_backend
from quarry.ingestion.image_prep import ImagePreparer
from quarry.ingestion.ingest_stats import IngestStats
from quarry.ingestion.streaming import DocumentStreamer, progressive_insert
from quarry.ingestion.text_format import TEXT_LIKE_FORMATS, TextLikeFormat
from quarry.ingestion.web_fetch import WebFetcher
from quarry.models import Chunk, PageContent, PageType
from quarry.results import IngestResult, SitemapResult

logger = logging.getLogger(__name__)

# The text-like formats reach the pipeline through ``TEXT_LIKE_FORMATS`` (its keys
# are their extensions), so the per-format extractor modules are imported there,
# not here.
SUPPORTED_EXTENSIONS = (
    frozenset({".pdf"}) | SUPPORTED_IMAGE_EXTENSIONS | frozenset(TEXT_LIKE_FORMATS)
)


_NO_STATS = IngestStats()


def ingest_document(
    file_path: Path,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest a document: dispatch to format-specific handler.

    Supported formats: PDF, TXT, MD, TEX, DOCX, HTML, PPTX, PNG, JPEG, TIFF,
    BMP, WebP, XLSX, CSV.

    Args:
        file_path: Path to the document.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name. Defaults to
            ``file_path.name``.  Pass a relative path (e.g. from
            ``file_path.relative_to(root)``) to avoid collisions when
            syncing directories with identically-named files in
            subdirectories.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.

    Raises:
        FileNotFoundError: If file does not exist.
        ValueError: If file format is not supported.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    suffix = file_path.suffix.lower()

    # Common kwargs for agent memory tagging, forwarded to all handlers.
    memory_kw = {
        "agent_handle": agent_handle,
        "memory_type": memory_type,
        "summary": summary,
    }

    if suffix == ".pdf":
        return ingest_pdf(
            file_path,
            database,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
            **memory_kw,
        )

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return ingest_image(
            file_path,
            database,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
            **memory_kw,
        )

    fmt = TEXT_LIKE_FORMATS.get(suffix)
    if fmt is not None:
        return _ingest_text_like(
            fmt,
            file_path,
            database,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=document_name,
            progress_callback=progress_callback,
            **memory_kw,
        )

    msg = f"Unsupported file format: {suffix}"
    raise ValueError(msg)


def ingest_pdf(
    file_path: Path,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest a PDF document: analyze, extract/OCR, chunk, embed, store.

    Args:
        file_path: Path to the PDF file.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results (pages, chunks, etc).
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Analyzing: %s", document_name)

    if overwrite:
        database.store.delete_document(
            document_name, collection=collection, count=False
        )

    ocr = get_ocr_backend(settings)
    extractor = PdfExtractor(settings, ocr)
    all_pages = extractor.extract_pages(file_path, document_name=document_name)
    total_pages = len(all_pages)
    text_pages = sum(1 for p in all_pages if p.page_type == PageType.TEXT)
    image_pages = sum(1 for p in all_pages if p.page_type == PageType.IMAGE)

    progress(
        "Pages: %d total, %d text, %d image",
        total_pages,
        text_pages,
        image_pages,
    )

    return _chunk_embed_store(
        all_pages,
        document_name,
        database,
        settings,
        progress,
        collection=collection,
        source_format=".pdf",
        stats=IngestStats(
            total_pages=total_pages,
            text_pages=text_pages,
            image_pages=image_pages,
        ),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def _ingest_text_like(
    fmt: TextLikeFormat,
    file_path: Path,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest one text-like document per *fmt*: extract pages, chunk, embed, store."""
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("%s: %s", fmt.read_verb, document_name)

    if overwrite:
        database.store.delete_document(
            document_name, collection=collection, count=False
        )

    pages = fmt.extract(settings, file_path, document_name)
    progress("%s: %d", fmt.unit_label, len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        database,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        stats=fmt.stats(len(pages)),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def ingest_image(
    file_path: Path,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest a standalone image: OCR, chunk, embed, store.

    Supported: PNG, JPEG, TIFF (multi-page), BMP, WebP.
    BMP and WebP are converted to PNG before OCR (required by most engines).

    Single-page images use the OCR backend's sync API. Multi-page TIFFs use
    the async API (S3 for cloud backends, local for on-device backends).

    Args:
        file_path: Path to image file.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name.
        progress_callback: Optional callable for progress messages.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)
    document_name = document_name or file_path.name

    progress("Analyzing image: %s", document_name)

    if overwrite:
        database.store.delete_document(
            document_name, collection=collection, count=False
        )

    analysis = ImageExtractor.analyze(file_path)
    progress(
        "Image: %s, %d pages, conversion=%s",
        analysis.format,
        analysis.page_count,
        analysis.needs_conversion,
    )

    if analysis.page_count > 1:
        return _ingest_multipage_image(
            file_path,
            analysis.page_count,
            database,
            settings,
            progress,
            document_name=document_name,
            collection=collection,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    image_bytes = ImagePreparer(
        file_path, needs_conversion=analysis.needs_conversion
    ).to_bytes()
    ocr = get_ocr_backend(settings)
    page = ocr.ocr_image_bytes(
        image_bytes,
        document_name=document_name,
        document_path=file_path.resolve(),
    )

    return _chunk_embed_store(
        [page],
        document_name,
        database,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        stats=IngestStats(file_format=analysis.format, image_pages=1),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def _ingest_multipage_image(
    file_path: Path,
    page_count: int,
    database: Database,
    settings: Settings,
    progress: Callable[..., None],
    *,
    document_name: str | None = None,
    collection: str = "default",
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest a multi-page image (TIFF) via the OCR backend's async path."""
    document_name = document_name or file_path.name
    progress("Running OCR on %d pages (async)", page_count)
    all_page_numbers = list(range(1, page_count + 1))
    ocr = get_ocr_backend(settings)
    pages = ocr.ocr_document(
        file_path, all_page_numbers, page_count, document_name=document_name
    )

    return _chunk_embed_store(
        pages,
        document_name,
        database,
        settings,
        progress,
        collection=collection,
        source_format=file_path.suffix.lower(),
        stats=IngestStats(file_format="TIFF", image_pages=page_count),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


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
        return HtmlExtractor().extract_from_html(content, document_name, document_name)
    return TextExtractor().extract_raw(content, document_name, format_hint=format_hint)


def ingest_content(
    content: str,
    document_name: str,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    format_hint: str = "auto",
    progress_callback: Callable[[str], None] | None = None,
    content_scrubber: Callable[[str], str] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Ingest inline content: split into sections, chunk, embed, store.

    Args:
        content: The text content to ingest.
        document_name: Name for the document.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: If True, delete existing data for this document first.
        collection: Collection name for organizing documents.
        format_hint: One of 'auto', 'plain', 'markdown', 'latex', 'html'.
        progress_callback: Optional callable for progress messages.
        content_scrubber: Optional redaction hook applied to each extracted page
            before chunking.  ``None`` (the default) stores the text
            byte-for-byte, so user-initiated remembers are unchanged; the capture
            ingress passes a scrubber so its collection never receives raw PII.
        agent_handle: Agent that owns this memory (empty for non-agent content).
        memory_type: Memory classification (fact, observation, opinion, procedure).
        summary: One-line summary of the content.

    Returns:
        Dict with ingestion results.
    """
    progress = _make_progress(progress_callback)

    if content_scrubber is not None:
        # Choke point: the scrubber's presence marks a scrubbed ingest, so the
        # free-form metadata the chunker copies onto every chunk — the document
        # name and the summary — is redacted HERE, once, for every scrubbed
        # caller (daemon capture/remember, stdio MCP, backfill).  No caller can
        # forget it and no new surface can reintroduce the leak.  A plain ingest
        # (no scrubber) stores metadata byte-for-byte, unchanged.
        document_name = content_scrubber(document_name)
        summary = content_scrubber(summary)

    progress("Processing: %s", document_name)

    pages = _extract_inline_pages(content, document_name, format_hint)
    if content_scrubber is not None:
        pages = [replace(page, text=content_scrubber(page.text)) for page in pages]

    # Delete the prior copy only after a successful scrub AND only when the new
    # extraction actually yielded content.  A scrub that raises aborts before
    # this line; an empty extraction (already-markdown, JS-only, or non-HTML)
    # must not replace a prior good capture with nothing — that would be silent
    # data loss reported as a fresh capture.
    if not pages:
        logger.warning(
            "ingest_content: %s extracted to zero pages — keeping any prior "
            "document, storing nothing",
            document_name,
        )
    elif overwrite:
        database.store.delete_document(
            document_name, collection=collection, count=False
        )
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        database,
        settings,
        progress,
        collection=collection,
        source_format="inline",
        stats=IngestStats(sections=len(pages)),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def ingest_url(
    url: str,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str | None = None,
    timeout: int = 30,
    delay: float = 0.0,
    progress_callback: Callable[[str], None] | None = None,
    content_scrubber: Callable[[str], str] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Fetch a URL, extract text from HTML, chunk, embed, store.

    Args:
        url: HTTP(S) URL to fetch.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: If True, delete existing data for this URL first.
        collection: Collection name for organizing documents.
        document_name: Override for the stored document name. Defaults to URL.
        timeout: HTTP request timeout in seconds.
        delay: Pre-fetch sleep in seconds plus 0-1s sub-second jitter, to avoid
            synchronized bursts when many workers fetch in parallel. 0 disables.
        progress_callback: Optional callable for progress messages.
        content_scrubber: Optional redaction hook applied to each extracted page
            before chunking. ``None`` (the default) stores the fetched text
            byte-for-byte, so user-initiated ingests are unchanged; the WebFetch
            auto-capture ingress passes a scrubber so its pushable collection
            never receives raw PII.

    Returns:
        Dict with ingestion results.

    Raises:
        ValueError: If URL is invalid, unreachable, or returns non-HTML.
    """
    progress = _make_progress(progress_callback)
    # A capture (content_scrubber set) must not persist userinfo/query/fragment
    # from the URL as document metadata; a plain ingest keeps the full URL.
    meta_url = (
        CaptureUrl(url).redacted(content_scrubber)
        if content_scrubber is not None
        else url
    )
    document_name = document_name or meta_url
    if content_scrubber is not None:
        # Same choke point as ingest_content: a scrubbed URL ingest redacts the
        # metadata the chunker copies onto every chunk.  CaptureUrl already
        # stripped userinfo/query/fragment from meta_url; this second pass
        # catches PII in an explicit document_name and in the summary, so no
        # caller has to scrub them itself.  Idempotent — a re-scrub of a redacted
        # value is a no-op.
        document_name = content_scrubber(document_name)
        summary = content_scrubber(summary)

    if delay:
        # Sub-second jitter from the monotonic clock (non-security-critical) to
        # desync parallel fetchers without importing random.
        jitter = time.monotonic_ns() % 1_000_000_000 / 1_000_000_000
        time.sleep(delay + jitter)

    progress("Fetching: %s", meta_url)
    html = WebFetcher(timeout).fetch(url)
    progress("Fetched %d characters", len(html))

    pages = HtmlExtractor().extract_from_html(html, document_name, meta_url)
    if content_scrubber is not None:
        pages = [replace(page, text=content_scrubber(page.text)) for page in pages]

    # Delete the prior copy only after a successful fetch+extraction+scrub AND
    # only when there are pages to store: an empty extraction or a scrub error
    # must not remove a good prior capture and store nothing (silent data loss).
    if not pages:
        logger.warning(
            "ingest_url: %s extracted to zero pages — keeping any prior "
            "document, storing nothing",
            meta_url,
        )
    elif overwrite:
        database.store.delete_document(
            document_name, collection=collection, count=False
        )
    progress("Sections: %d", len(pages))

    return _chunk_embed_store(
        pages,
        document_name,
        database,
        settings,
        progress,
        collection=collection,
        source_format=".html",
        stats=IngestStats(sections=len(pages)),
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def _entry_is_current(existing_ts: str, lastmod: datetime) -> bool:
    """Return True if *lastmod* is not newer than the stored *existing_ts*.

    An unparseable stored timestamp returns False so the URL is re-ingested
    rather than silently skipped.
    """
    try:
        existing_dt = datetime.fromisoformat(str(existing_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if existing_dt.tzinfo is None:
        existing_dt = existing_dt.replace(tzinfo=UTC)
    return lastmod <= existing_dt


def _bulk_ingest_entries(
    entries: list[SitemapEntry],
    database: Database,
    settings: Settings,
    *,
    source_url: str,
    collection: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    limit: int = 0,
    overwrite: bool = False,
    workers: int = 4,
    delay: float = 0.5,
    timeout: int = 30,
    progress: Callable[..., None],
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> SitemapResult:
    """Filter, dedup, and parallel-ingest a list of sitemap entries.

    Shared by ``ingest_sitemap`` (explicit sitemap URL) and
    ``ingest_auto`` (auto-discovered pages).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

    workers = max(1, workers)
    total_discovered = len(entries)

    filtered = SitemapDiscovery.filter_entries(
        entries,
        include=include,
        exclude=exclude,
        limit=limit,
    )
    after_filter = len(filtered)
    progress("After filtering: %d URLs", after_filter)

    # Build lookup of existing documents for lastmod dedup
    existing_docs = database.catalog.list_documents(collection_filter=collection)
    existing_timestamps: dict[str, str] = {
        doc["document_name"]: doc["ingestion_timestamp"] for doc in existing_docs
    }

    # Determine which URLs to ingest vs skip
    to_ingest: list[tuple[str, str | None]] = []
    skipped = 0

    for entry in filtered:
        existing_ts = existing_timestamps.get(entry.loc)
        if (
            existing_ts
            and not overwrite
            and entry.lastmod is not None
            and _entry_is_current(existing_ts, entry.lastmod)
        ):
            skipped += 1
            continue
        to_ingest.append((entry.loc, None))

    progress("%d to ingest, %d up-to-date", len(to_ingest), skipped)

    ingested = 0
    failed = 0
    errors: list[str] = []

    if to_ingest:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    ingest_url,
                    page_url,
                    database,
                    settings,
                    # Always replace existing chunks for URLs that passed dedup.
                    # The dedup logic above already skipped unchanged URLs.
                    overwrite=True,
                    collection=collection,
                    document_name=doc_name,
                    timeout=timeout,
                    delay=delay,
                    agent_handle=agent_handle,
                    memory_type=memory_type,
                    summary=summary,
                ): page_url
                for page_url, doc_name in to_ingest
            }
            for future in as_completed(futures):
                page_url = futures[future]
                try:
                    future.result()
                    ingested += 1
                    progress(
                        "Ingested %s (%d/%d)",
                        page_url,
                        ingested,
                        len(to_ingest),
                    )
                except Exception as exc:
                    failed += 1
                    errors.append(f"{page_url}: {exc}")
                    logger.exception("Failed to ingest %s", page_url)
                    progress("Failed %s: %s", page_url, exc)

    progress(
        "Done: %d ingested, %d skipped, %d failed",
        ingested,
        skipped,
        failed,
    )

    return SitemapResult(
        sitemap_url=source_url,
        collection=collection,
        total_discovered=total_discovered,
        after_filter=after_filter,
        ingested=ingested,
        skipped=skipped,
        failed=failed,
        errors=errors,
    )


def ingest_sitemap(
    url: str,
    database: Database,
    settings: Settings,
    *,
    collection: str = "",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    limit: int = 0,
    overwrite: bool = False,
    workers: int = 4,
    delay: float = 0.5,
    timeout: int = 30,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> SitemapResult:
    """Crawl a sitemap and ingest all discovered URLs.

    Fetches the sitemap, discovers all URLs (following sitemap indexes),
    applies include/exclude filters, deduplicates against existing documents
    via <lastmod>, and ingests new/changed URLs in parallel.

    Args:
        url: Sitemap URL.
        database: Quarry database facade.
        settings: Application settings.
        collection: Collection name. Defaults to sitemap URL domain.
        include: URL path glob patterns to include (repeatable).
        exclude: URL path glob patterns to exclude (repeatable).
        limit: Max URLs to ingest (0 = no limit).
        overwrite: Force re-ingest regardless of <lastmod>.
        workers: Parallel fetch workers (minimum 1).
        delay: Base delay in seconds between fetches per worker
            (adds 0-1.0s sub-second jitter). Default 0.5s.
        timeout: HTTP timeout in seconds.
        progress_callback: Optional callable for progress messages.

    Returns:
        SitemapResult with counts and error details.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

    progress = _make_progress(progress_callback)

    if not collection:
        collection = urlparse(url).hostname or "default"

    progress("Fetching sitemap: %s", url)
    entries = SitemapDiscovery.discover_urls(url)
    progress("Discovered %d URLs", len(entries))

    return _bulk_ingest_entries(
        entries,
        database,
        settings,
        source_url=url,
        collection=collection,
        include=include,
        exclude=exclude,
        limit=limit,
        overwrite=overwrite,
        workers=workers,
        delay=delay,
        timeout=timeout,
        progress=progress,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def ingest_auto(
    url: str,
    database: Database,
    settings: Settings,
    *,
    overwrite: bool = False,
    collection: str = "",
    workers: int = 4,
    delay: float = 0.5,
    timeout: int = 30,
    progress_callback: Callable[[str], None] | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult | SitemapResult:
    """Smart URL ingestion: discover sitemap, crawl if found, else single page.

    Uses ultimate-sitemap-parser (USP) for robust sitemap discovery via
    robots.txt, well-known locations, and multiple sitemap formats
    (XML, RSS, Atom, plain text).

    1. If the URL is itself a sitemap, crawl it directly.
    2. Otherwise, auto-discover sitemaps for the site origin.
    3. If pages found, apply path-prefix filter and bulk-ingest.
    4. If no sitemap found, fall back to single-page ingestion.

    Args:
        url: Any HTTP(S) URL on the target site.
        database: Quarry database facade.
        settings: Application settings.
        overwrite: Force re-ingest regardless of existing data.
        collection: Collection name. Defaults to the URL hostname.
        workers: Parallel fetch workers for sitemap crawl (default 4).
        delay: Base delay between fetches in seconds (default 0.5).
        timeout: HTTP timeout in seconds.
        progress_callback: Optional callable for progress messages.

    Returns:
        SitemapResult if a sitemap was discovered, IngestResult otherwise.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

    progress = _make_progress(progress_callback)
    parsed = urlparse(url)

    if not collection:
        collection = parsed.hostname or "default"

    # If the URL itself is a sitemap, skip discovery and crawl directly.
    # Match sitemap files (*.xml, *.xml.gz) and /sitemap paths, but not
    # pages that merely contain "sitemap" as a substring (e.g. /sitemap-guide).
    path_lower = parsed.path.lower()
    last_segment = path_lower.rsplit("/", 1)[-1]
    is_sitemap = last_segment.startswith("sitemap") and (
        last_segment.endswith((".xml", ".xml.gz", ".txt")) or last_segment == "sitemap"
    )
    if is_sitemap:
        progress("URL is a sitemap, crawling directly")
        return ingest_sitemap(
            url,
            database,
            settings,
            collection=collection,
            overwrite=overwrite,
            workers=workers,
            delay=delay,
            timeout=timeout,
            progress_callback=progress_callback,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    progress("Discovering sitemaps for %s://%s", parsed.scheme, parsed.netloc)
    try:
        entries = SitemapDiscovery.discover_pages(url)
    except Exception:
        logger.exception("Sitemap discovery failed for %s", url)
        entries = []

    if not entries:
        progress("No sitemap found, ingesting single page")
        return ingest_url(
            url,
            database,
            settings,
            overwrite=overwrite,
            collection=collection,
            timeout=timeout,
            progress_callback=progress_callback,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    progress("Discovered %d pages via sitemap", len(entries))

    # Derive include filter from input URL path
    path = parsed.path.rstrip("/")
    include: list[str] | None = None
    if path:
        include = [path, f"{path}/*"]

    # Apply include filter once up front to avoid double-filtering
    # inside _bulk_ingest_entries.
    from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

    if include:
        entries = SitemapDiscovery.filter_entries(entries, include=include)

    # If filtering dropped everything, fall back to single-page ingestion.
    # This handles sites whose sitemap is partially parseable but doesn't
    # contain the requested path (e.g. namespace-prefixed XML).
    if not entries:
        progress("Sitemap has no pages matching %s, ingesting single page", path)
        return ingest_url(
            url,
            database,
            settings,
            overwrite=overwrite,
            collection=collection,
            timeout=timeout,
            progress_callback=progress_callback,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )

    return _bulk_ingest_entries(
        entries,
        database,
        settings,
        source_url=url,
        collection=collection,
        overwrite=overwrite,
        workers=workers,
        delay=delay,
        timeout=timeout,
        progress=progress,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


def _make_progress(
    callback: Callable[[str], None] | None,
) -> Callable[..., None]:
    """Create a progress reporter that logs and optionally calls a callback."""

    def _progress(fmt: str, *args: object) -> None:
        logger.info(fmt, *args)
        if callback is not None:
            callback(fmt % args if args else fmt)

    return _progress


def _chunk_embed_store(
    pages: list[PageContent],
    document_name: str,
    database: Database,
    settings: Settings,
    progress: Callable[..., None],
    *,
    collection: str = "default",
    source_format: str = "",
    stats: IngestStats = _NO_STATS,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> IngestResult:
    """Shared pipeline: chunk pages, embed in bounded windows, store progressively."""
    progress("Chunking")
    t0 = time.perf_counter()
    chunks = DocumentStreamer(settings).build_chunks(
        pages,
        collection=collection,
        source_format=source_format,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )
    logger.info(
        "pipeline: chunked %d pages → %d chunks in %.2fs",
        len(pages),
        len(chunks),
        time.perf_counter() - t0,
    )
    progress("Created %d chunks", len(chunks))

    if chunks:
        progress("Embedding + storing in bounded windows")
        t0 = time.perf_counter()
        inserted = progressive_insert(chunks, database.store, settings, document_name)
        logger.info(
            "pipeline: embedded + stored %d chunks in %.2fs",
            inserted,
            time.perf_counter() - t0,
        )
        progress("Done: %d chunks indexed from %s", inserted, document_name)
    else:
        inserted = 0
        progress("No text found — nothing to index")

    result: IngestResult = {
        "document_name": document_name,
        "collection": collection,
        "chunks": inserted,
    }
    # IngestResult keys are literal-typed, so a variable-key assignment can't be
    # expressed directly; cast the runtime-filtered subset of present fields.
    result.update(cast("IngestResult", stats.as_result_fields()))
    return result


def plan_file_chunks(
    file_path: Path,
    settings: Settings,
    *,
    collection: str = "default",
    document_name: str | None = None,
    agent_handle: str = "",
    memory_type: str = "",
    summary: str = "",
) -> tuple[list[Chunk], bool]:
    """Extract and chunk a file for sync, reporting extraction determinism.

    Returns ``(chunks, deterministic)``: the full ordered chunk list (embedding
    deferred) so the sync producer knows the document's total chunk count before
    streaming windows, plus whether the extraction is deterministic so resume can
    decide whether to trust a mid-file watermark (DES-034 §5.3, G3).

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        ValueError: If the file format is not supported.
    """
    if not file_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)
    document_name = document_name or file_path.name
    suffix = file_path.suffix.lower()
    pages = _extract_pages(file_path, suffix, document_name, settings)
    deterministic = _pages_are_deterministic(pages)
    if not pages:
        return [], deterministic
    chunks = DocumentStreamer(settings).build_chunks(
        pages,
        collection=collection,
        source_format=suffix,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )
    return chunks, deterministic


def _pages_are_deterministic(pages: list[PageContent]) -> bool:
    """Return False if any page came from OCR (``PageType.IMAGE``), else True.

    OCR (rapidocr) re-segments text differently on re-run, so a document with any
    OCR'd page cannot honor a within-file resume watermark (DES-034 §5.3, G3).
    """
    return not any(page.page_type == PageType.IMAGE for page in pages)


def _extract_image_pages(
    file_path: Path,
    document_name: str,
    settings: Settings,
) -> list[PageContent]:
    """Extract pages from an image file."""
    analysis = ImageExtractor.analyze(file_path)
    if analysis.page_count > 1:
        ocr = get_ocr_backend(settings)
        return ocr.ocr_document(
            file_path,
            list(range(1, analysis.page_count + 1)),
            analysis.page_count,
            document_name=document_name,
        )
    image_bytes = ImagePreparer(
        file_path, needs_conversion=analysis.needs_conversion
    ).to_bytes()
    ocr = get_ocr_backend(settings)
    return [
        ocr.ocr_image_bytes(
            image_bytes,
            document_name=document_name,
            document_path=file_path.resolve(),
        )
    ]


def _extract_pages(
    file_path: Path,
    suffix: str,
    document_name: str,
    settings: Settings,
) -> list[PageContent]:
    """Extract pages from a file based on its suffix."""
    if suffix == ".pdf":
        ocr = get_ocr_backend(settings)
        return PdfExtractor(settings, ocr).extract_pages(
            file_path, document_name=document_name
        )
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return _extract_image_pages(file_path, document_name, settings)
    fmt = TEXT_LIKE_FORMATS.get(suffix)
    if fmt is not None:
        return fmt.extract(settings, file_path, document_name)

    msg = f"Unsupported file format: {suffix}"
    raise ValueError(msg)
