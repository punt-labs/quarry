"""Sitemap-driven and auto-discovered bulk URL ingestion."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypedDict, final

from quarry.ingest_collection import IngestCollection
from quarry.ingestion.bulk_ingest import BulkIngestRunner, BulkOptions
from quarry.ingestion.crawl_guard import CrawlGuard
from quarry.ingestion.existing_page import ExistingPage
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.safe_entry_selector import SafeEntrySelector
from quarry.ingestion.web_ingest import UrlIngest, ingest_url

if TYPE_CHECKING:
    from quarry.http_url import HttpUrl
    from quarry.ingestion.ingest_context import IngestContext, Progress
    from quarry.results import IngestResult
    from quarry.sitemap import SitemapEntry

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class SitemapCrawl:
    """A discovered set of sitemap entries plus their originating URL."""

    entries: list[SitemapEntry]
    source_url: HttpUrl


class SitemapResult(TypedDict):
    """Result of sitemap crawl and bulk URL ingestion."""

    sitemap_url: str
    collection: str
    total_discovered: int
    after_filter: int
    ingested: int
    empty: int
    skipped: int
    failed: int
    errors: list[str]


@final
class SitemapIngest:
    """Sitemap-driven and auto-discovered bulk URL ingestion, funneled through
    :class:`BulkIngestRunner`.
    """

    @staticmethod
    def _bulk_ingest_entries(
        crawl: SitemapCrawl,
        progress: Progress,
        context: IngestContext,
        options: BulkOptions,
    ) -> SitemapResult:
        """Filter, dedup, and parallel-ingest a list of sitemap entries.

        Shared by ``ingest_sitemap`` (explicit sitemap URL) and
        ``ingest_auto`` (auto-discovered pages).
        """
        selector = SafeEntrySelector(
            include=options.include, exclude=options.exclude, limit=options.limit
        )
        filtered = selector.select_safe(crawl.entries)
        CrawlGuard.of(options).require_safe(len(crawl.entries), len(filtered))
        progress("Selected %d safe URLs to ingest", len(filtered))
        to_ingest, skipped = selector.partition(filtered, context)
        progress("%d to ingest, %d up-to-date", len(to_ingest), skipped)
        ingested, failed, errors, empty = BulkIngestRunner(
            ingest_one=ingest_url, workers=options.workers
        ).run(to_ingest, progress, context, options)
        progress(
            "Done: %d ingested, %d empty, %d skipped, %d failed",
            ingested,
            empty,
            skipped,
            failed,
        )

        return SitemapResult(
            sitemap_url=crawl.source_url.document_name,
            collection=context.collection,
            total_discovered=len(crawl.entries),
            after_filter=len(filtered),
            ingested=ingested,
            empty=empty,
            skipped=skipped,
            failed=failed,
            errors=errors,
        )

    @staticmethod
    def ingest_sitemap(
        url: HttpUrl,
        progress: Progress,
        context: IngestContext,
        options: BulkOptions,
    ) -> SitemapResult:
        """Crawl a sitemap and ingest all discovered URLs.

        Fetches the sitemap, discovers all URLs (following sitemap indexes),
        applies include/exclude filters, deduplicates against existing documents
        via <lastmod>, and ingests new/changed URLs in parallel.

        Args:
            url: Sitemap URL.
            progress: Progress reporter.
            context: Shared database/settings/overwrite/memory-tag knobs.
                ``context.collection`` derives the URL hostname when empty
                (``""``); any other value -- including the literal
                ``"default"`` -- is used verbatim (DES-042).
            options: Worker count, delay, timeout, and include/exclude/limit.

        Returns:
            SitemapResult with counts and error details.
        """
        from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

        # The daemon route (daemon/routes/ingestion.py) resolves the queue
        # key -- the FIFO worker this job serializes on -- through this SAME
        # resolver BEFORE building the job, so context.collection always
        # arrives here already resolved: hostname-derived when the caller
        # omitted --collection, or the caller's exact chosen name otherwise,
        # including a literal "default" when the caller asked for it
        # (DES-042 single-writer-per-table). Passing context.collection
        # through verbatim -- rather than treating the string "default" as a
        # second "let the pipeline decide" sentinel -- keeps this call's
        # write target locked to that already-serialized key; re-resolving
        # an already-resolved name is a no-op. An empty context.collection
        # (any future caller that genuinely means "unspecified") still
        # derives the hostname, per IngestCollection.resolve's contract.
        context = replace(
            context, collection=IngestCollection.resolve(url, context.collection).name
        )

        progress("Fetching sitemap: %s", url)
        entries = SitemapDiscovery.discover_urls(url)
        progress("Discovered %d URLs", len(entries))

        return SitemapIngest._bulk_ingest_entries(
            SitemapCrawl(entries=entries, source_url=url), progress, context, options
        )

    @staticmethod
    def _single_page(
        url: HttpUrl, progress: Progress, context: IngestContext, options: BulkOptions
    ) -> IngestResult:
        """Ingest one page; without ``overwrite`` that is create-if-absent.

        A page already stored under its name is reported as skipped, the way the
        content jobs do, instead of being fetched and written on top of itself.
        """
        if existing := ExistingPage.page(url.document_name, context):
            progress("Already stored, skipping")
            return existing.skip()
        return ingest_url(
            UrlIngest(url=url, timeout=options.timeout), progress, context
        )

    @staticmethod
    def ingest_auto(
        url: HttpUrl,
        progress: Progress,
        context: IngestContext,
        options: BulkOptions,
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
            progress: Progress reporter.
            context: Shared database/settings/overwrite/memory-tag knobs.
                ``context.collection`` derives the URL hostname when empty
                (``""``); any other value -- including the literal
                ``"default"`` -- is used verbatim (DES-042).
            options: Worker count, delay, and timeout for a sitemap crawl.

        Returns:
            SitemapResult if a sitemap was discovered, IngestResult otherwise.
        """
        from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

        # See ingest_sitemap's matching comment: context.collection is passed
        # through verbatim -- the daemon route already resolved it to the
        # queue key this job serializes on, and re-resolving that name is a
        # no-op; only a genuinely empty collection derives the hostname here.
        context = replace(
            context, collection=IngestCollection.resolve(url, context.collection).name
        )

        # If the URL itself is a sitemap, skip discovery and crawl directly.
        # Match sitemap files (*.xml, *.xml.gz) and /sitemap paths, but not
        # pages that merely contain "sitemap" as a substring (e.g. /sitemap-guide).
        path_lower = url.path.lower()
        last_segment = path_lower.rsplit("/", 1)[-1]
        is_sitemap = last_segment.startswith("sitemap") and (
            last_segment.endswith((".xml", ".xml.gz", ".txt"))
            or last_segment == "sitemap"
        )
        if is_sitemap:
            progress("URL is a sitemap, crawling directly")
            return SitemapIngest.ingest_sitemap(url, progress, context, options)

        progress("Discovering sitemaps for %s", url.homepage)
        try:
            entries = SitemapDiscovery.discover_pages(url)
        except FetchRefusedError as exc:
            # The reader replaces USP's own documented failure modes -- a
            # malformed homepage URL, unparseable XML, a corrupt gzip body --
            # with a URL-free refusal (their messages embed the raw URL).  That
            # is the sitemap being genuinely absent or broken, so this still
            # falls back to a single-page ingest below.  Anything else is a
            # programmer error and must propagate, not be swallowed as "no
            # sitemap found."  The URL is redacted for the log (CWE-532); this
            # is an expected fallback, so logger.warning, not .exception.
            logger.warning("Sitemap discovery failed for %s: %s", url, exc)
            progress("Sitemap discovery error for %s", url)
            entries = []

        if not entries:
            progress("No sitemap found, ingesting single page")
            return SitemapIngest._single_page(url, progress, context, options)

        progress("Discovered %d pages via sitemap", len(entries))

        # Derive include filter from input URL path, applied once up front to
        # avoid double-filtering inside _bulk_ingest_entries.
        path = url.path.rstrip("/")
        if path:
            entries = SitemapDiscovery.filter_entries(
                entries, include=[path, f"{path}/*"]
            )

        # If filtering dropped everything, fall back to single-page ingestion.
        # This handles sites whose sitemap is partially parseable but doesn't
        # contain the requested path (e.g. namespace-prefixed XML).
        if not entries:
            progress("Sitemap has no pages matching the path, ingesting single page")
            return SitemapIngest._single_page(url, progress, context, options)

        return SitemapIngest._bulk_ingest_entries(
            SitemapCrawl(entries=entries, source_url=url), progress, context, options
        )


# Module-level aliases: every existing caller and the package's public API spell
# these ``ingest_sitemap(...)`` and ``ingest_auto(...)`` (no receiver) — binding
# the class's staticmethods to bare module names keeps that calling convention
# while the funnel's entry points live as real, class-scoped methods above.
ingest_sitemap = SitemapIngest.ingest_sitemap
ingest_auto = SitemapIngest.ingest_auto

__all__ = ["SitemapCrawl", "SitemapIngest", "ingest_auto", "ingest_sitemap"]
