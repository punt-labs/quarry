"""Fetch sitemaps through the gated client, failing loudly and logging redacted URLs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from usp.objects.sitemap import InvalidSitemap

from quarry.capture_url import CaptureUrl
from quarry.ingestion.fetched_body import FetchRefusedError
from quarry.sitemap_web_client import GatedSitemapWebClient

if TYPE_CHECKING:
    from usp.objects.page import SitemapPage

logger = logging.getLogger(__name__)


@final
class SitemapReader:
    """Read the pages a sitemap lists via ultimate-sitemap-parser (USP).

    Every fetch goes through :class:`GatedSitemapWebClient`, so each hop passes
    the SSRF gate.  URLs reach the log only in redacted form (CWE-532), and a
    sitemap that could not be fetched or parsed raises instead of reading as an
    empty crawl.
    """

    __slots__ = ("_client",)

    _client: GatedSitemapWebClient

    def __new__(cls, client: GatedSitemapWebClient | None = None) -> Self:
        self = super().__new__(cls)
        self._client = client or GatedSitemapWebClient()
        return self

    @staticmethod
    def shown(url: str) -> str:
        """Return *url* as it may appear in a log line: redacted, never raising."""
        return CaptureUrl.display(url)

    def discover(self, homepage: str) -> list[SitemapPage]:
        """Return every page found by sitemap discovery for *homepage*."""
        from usp.tree import sitemap_tree_for_homepage  # noqa: PLC0415

        logger.info("Discovering sitemaps for %s", self.shown(homepage))
        tree = sitemap_tree_for_homepage(homepage, web_client=self._client)
        return list(tree.all_pages())

    def read(self, url: str) -> list[SitemapPage]:
        """Return the pages of the sitemap at *url*, recursing into indexes.

        Raises:
            FetchRefusedError: The sitemap could not be fetched or parsed.  The
                message omits the URL, which USP's own reason embeds.
        """
        from usp.fetch_parse import SitemapFetcher  # noqa: PLC0415

        shown = self.shown(url)
        logger.info("Fetching sitemap: %s", shown)
        sitemap = SitemapFetcher(
            url=url, recursion_level=0, web_client=self._client
        ).sitemap()
        if isinstance(sitemap, InvalidSitemap):
            msg = "fetch failed: sitemap could not be fetched or parsed"
            raise FetchRefusedError(msg)
        pages = list(sitemap.all_pages())
        logger.info("Parsed %d pages from %s", len(pages), shown)
        return pages
