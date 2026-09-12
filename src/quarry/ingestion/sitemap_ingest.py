"""Sitemap-driven and auto-discovered bulk URL ingestion."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, final

from quarry.capture_url import CaptureUrl
from quarry.ingest_collection import IngestCollection
from quarry.ingestion.bulk_ingest import (
    BulkIngestRunner,
    BulkOptions,
    SafeEntrySelector,
)
from quarry.ingestion.web_ingest import UrlIngest, ingest_url
from quarry.results import SitemapResult
from quarry.scrub import scrub_and_log

if TYPE_CHECKING:
    from quarry.ingestion.ingest_context import IngestContext, Progress
    from quarry.results import IngestResult
    from quarry.sitemap import SitemapEntry

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class SitemapCrawl:
    """A discovered set of sitemap entries plus their originating URL."""

    entries: list[SitemapEntry]
    source_url: str


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
        progress("Selected %d safe URLs to ingest", len(filtered))
        to_ingest, skipped = selector.partition(filtered, context)
        progress("%d to ingest, %d up-to-date", len(to_ingest), skipped)
        ingested, failed, errors = BulkIngestRunner(
            ingest_one=ingest_url, workers=options.workers
        ).run(to_ingest, progress, context, options)
        progress("Done: %d ingested, %d skipped, %d failed", ingested, skipped, failed)

        return SitemapResult(
            sitemap_url=crawl.source_url,
            collection=context.collection,
            total_discovered=len(crawl.entries),
            after_filter=len(filtered),
            ingested=ingested,
            skipped=skipped,
            failed=failed,
            errors=errors,
        )

    @staticmethod
    def ingest_sitemap(
        url: str,
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

        # url itself is only for the discover_urls/IngestCollection.resolve
        # calls above and below (the actual fetch/routing inputs) and for
        # SitemapCrawl.source_url (returned data, not a log sink); the
        # progress line gets the redacted form so a userinfo/query secret on
        # the sitemap URL never reaches quarry.log (CWE-532).
        progress("Fetching sitemap: %s", CaptureUrl(url).redacted(lambda text: text))
        entries = SitemapDiscovery.discover_urls(url)
        progress("Discovered %d URLs", len(entries))

        return SitemapIngest._bulk_ingest_entries(
            SitemapCrawl(entries=entries, source_url=url), progress, context, options
        )

    @staticmethod
    def ingest_auto(
        url: str,
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
        from urllib.parse import urlparse  # noqa: PLC0415

        from usp.exceptions import (  # noqa: PLC0415
            GunzipException,
            SitemapException,
            SitemapXMLParsingException,
            StripURLToHomepageException,
        )

        from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

        parsed = urlparse(url)
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
        path_lower = parsed.path.lower()
        last_segment = path_lower.rsplit("/", 1)[-1]
        is_sitemap = last_segment.startswith("sitemap") and (
            last_segment.endswith((".xml", ".xml.gz", ".txt"))
            or last_segment == "sitemap"
        )
        if is_sitemap:
            progress("URL is a sitemap, crawling directly")
            return SitemapIngest.ingest_sitemap(url, progress, context, options)

        # parsed.netloc can carry userinfo ("user:pass@host") -- re-derive the
        # netloc from the redacted URL so a credential on the origin never
        # reaches the progress stream or the log (CWE-532); scheme alone is
        # never sensitive.
        safe_netloc = urlparse(CaptureUrl(url).redacted(lambda text: text)).netloc
        progress("Discovering sitemaps for %s://%s", parsed.scheme, safe_netloc)
        try:
            entries = SitemapDiscovery.discover_pages(url)
        except (
            SitemapException,
            SitemapXMLParsingException,
            GunzipException,
            StripURLToHomepageException,
        ) as exc:
            # USP's own documented failure modes -- a malformed homepage URL,
            # unparseable XML, a corrupt gzip body -- are the sitemap being
            # genuinely absent or broken, so this still falls back to a
            # single-page ingest below.  Anything else is a programmer error
            # and must propagate, not be swallowed as "no sitemap found."
            #
            # usp's own exception messages embed the raw URL verbatim (e.g.
            # "URL {url} is not a HTTP(s) URL") -- str(exc) is NOT safe to
            # log or report, and neither is the raw url: either would carry
            # ?token= query params or user:pass@ userinfo into the persistent
            # quarry.log or the caller's progress stream (CWE-532). Redact
            # through the same CaptureUrl machinery web_ingest.py uses for
            # capture URLs, and log only the exception CLASS, not its message.
            # This is an expected fallback, not a bug -- logger.warning, not
            # .exception, so no stack trace.
            redacted_url = CaptureUrl(url).redacted(
                lambda text: scrub_and_log(text, "sitemap-discovery")
            )
            logger.warning(
                "Sitemap discovery failed for %s (%s)", redacted_url, type(exc).__name__
            )
            progress("Sitemap discovery error for %s", redacted_url)
            entries = []

        if not entries:
            progress("No sitemap found, ingesting single page")
            request = UrlIngest(url=url, timeout=options.timeout)
            return ingest_url(request, progress, context)

        progress("Discovered %d pages via sitemap", len(entries))

        # Derive include filter from input URL path, applied once up front to
        # avoid double-filtering inside _bulk_ingest_entries.
        path = parsed.path.rstrip("/")
        if path:
            entries = SitemapDiscovery.filter_entries(
                entries, include=[path, f"{path}/*"]
            )

        # If filtering dropped everything, fall back to single-page ingestion.
        # This handles sites whose sitemap is partially parseable but doesn't
        # contain the requested path (e.g. namespace-prefixed XML).
        if not entries:
            progress("Sitemap has no pages matching %s, ingesting single page", path)
            request = UrlIngest(url=url, timeout=options.timeout)
            return ingest_url(request, progress, context)

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
