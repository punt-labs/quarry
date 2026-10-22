"""Sitemap discovery and parsing via ultimate-sitemap-parser (USP).

USP handles robots.txt discovery, well-known sitemap locations, recursive
sitemap indexes, and multiple formats (XML, RSS, Atom, plain text) with
error tolerance for malformed content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from quarry.ingestion.sitemap_reader import SitemapReader
from quarry.url_admission import UrlAdmission

if TYPE_CHECKING:
    from quarry.http_url import HttpUrl

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """A URL discovered from a sitemap."""

    loc: str
    lastmod: datetime | None


class SitemapDiscovery:
    """Sitemap crawling, parsing, and URL filtering."""

    @staticmethod
    def _pages_to_entries(
        pages: object,
    ) -> list[SitemapEntry]:
        """Convert USP SitemapPage objects to SitemapEntry, deduplicating by URL."""
        from usp.objects.page import SitemapPage  # noqa: PLC0415

        seen: set[str] = set()
        entries: list[SitemapEntry] = []
        for page in pages:  # type: ignore[attr-defined]
            if not isinstance(page, SitemapPage):
                continue
            if page.url not in seen:
                seen.add(page.url)
                entries.append(SitemapEntry(loc=page.url, lastmod=page.last_modified))
        return entries

    @staticmethod
    def discover_pages(url: HttpUrl) -> list[SitemapEntry]:
        """Discover all pages for a website via sitemap auto-discovery.

        Uses USP's ``sitemap_tree_for_homepage`` to probe robots.txt and
        well-known sitemap locations, then parse all discovered sitemaps
        (XML, RSS, Atom, plain text) with error tolerance.

        Args:
            url: Any HTTP(S) URL on the target site. The origin is extracted
                and used as the homepage for discovery.

        Returns:
            Deduplicated list of all discovered pages.
        """
        return SitemapDiscovery._pages_to_entries(
            SitemapReader().discover(url.homepage)
        )

    @staticmethod
    def discover_urls(url: HttpUrl) -> list[SitemapEntry]:
        """Fetch and parse a specific sitemap URL, recursing into indexes.

        Uses USP's ``SitemapFetcher`` for robust parsing of XML, RSS, Atom,
        and plain text sitemaps with error tolerance.

        Args:
            url: Sitemap URL to fetch and parse.

        Returns:
            Deduplicated flat list of all SitemapEntry found.

        Raises:
            FetchRefusedError: The sitemap could not be fetched or parsed, or the
                parser rejected *url*.  The message names no URL.
        """
        return SitemapDiscovery._pages_to_entries(SitemapReader().read(url))

    @staticmethod
    def _matches(
        entry: SitemapEntry,
        include: list[str] | None,
        exclude: list[str] | None,
    ) -> bool:
        """Return whether *entry*'s path passes the include/exclude globs.

        Exclude takes precedence over include; no patterns means match-all.
        """
        path = urlparse(entry.loc).path
        if exclude and any(fnmatch(path, pat) for pat in exclude):
            return False
        return not (include and not any(fnmatch(path, pat) for pat in include))

    @staticmethod
    def select_safe(
        entries: list[SitemapEntry],
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        limit: int = 0,
    ) -> list[SitemapEntry]:
        """Glob-filter, SSRF-gate, and cap the entries in ONE lazy pass.

        A sitemap is attacker-controlled content: its entries may point at
        internal addresses (link-local, loopback, RFC-1918, CGNAT, metadata).
        Every considered entry is gated against its resolved address -- the same
        check the ingest route runs on the initial source -- and an unsafe or
        unresolvable URL is dropped and logged rather than fetched, so a public
        sitemap listing internal URLs triggers no internal fetch.  Complementary
        to pinning the resolved IP: this gates each listed URL, not the
        connection.

        Fusing glob-filter, gate, and cap into one pass keeps the added work
        proportional to the result, not the sitemap: with *limit* > 0 the scan
        stops once *limit* SAFE entries are gathered, so ``urlparse`` (glob) and
        DNS resolution (gate) run only ~enough times to fill the limit rather
        than across the whole (possibly huge) sitemap.  The limit thus counts
        SAFE pages -- not *limit* minus the unsafe ones that happened to sort
        first.  With *limit* == 0 every matching entry is gated.
        """
        safe: list[SitemapEntry] = []
        for entry in entries:
            if not SitemapDiscovery._matches(entry, include, exclude):
                continue
            reason = UrlAdmission.reject_reason(entry.loc)
            if reason is not None:
                logger.warning(
                    "Dropping unsafe sitemap URL %s: %s",
                    SitemapReader.shown(entry.loc),
                    reason,
                )
                continue
            safe.append(entry)
            if limit > 0 and len(safe) >= limit:
                break
        return safe

    @staticmethod
    def filter_entries(
        entries: list[SitemapEntry],
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        limit: int = 0,
    ) -> list[SitemapEntry]:
        """Filter sitemap entries by URL path glob patterns.

        Args:
            entries: Sitemap entries to filter.
            include: If provided, only URLs whose path matches at least one pattern.
            exclude: URLs whose path matches any pattern are removed.
                Exclude takes precedence over include.
            limit: Maximum entries to return (0 = no limit).

        Returns:
            Filtered list of entries.
        """
        result: list[SitemapEntry] = []
        for entry in entries:
            if not SitemapDiscovery._matches(entry, include, exclude):
                continue
            result.append(entry)
            if limit > 0 and len(result) >= limit:
                break
        return result
