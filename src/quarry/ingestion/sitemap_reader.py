"""Fetch sitemaps through the gated client, failing loudly and logging redacted URLs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from usp.exceptions import (
    GunzipException,
    SitemapException,
    SitemapXMLParsingException,
    StripURLToHomepageException,
)
from usp.objects.sitemap import InvalidSitemap

from quarry.capture_url import CaptureUrl
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.sitemap_web_client import GatedSitemapWebClient

if TYPE_CHECKING:
    from usp.objects.page import SitemapPage

    from quarry.http_url import HttpUrl

logger = logging.getLogger(__name__)

# The parser's messages quote the URL verbatim, so each failure it can raise is
# replaced by a fixed refusal, never chained or re-raised; only the type is
# logged.  Anything outside this set is a programming error: it propagates to
# the task terminal, which records only its class (internal error: <Class>).
_PARSER_FAILURES = (
    SitemapException,
    SitemapXMLParsingException,
    GunzipException,
    StripURLToHomepageException,
    ValueError,
    OSError,
)
_PARSER_REFUSAL = "fetch failed: sitemap URL not accepted by the parser"
_UNREADABLE = "fetch failed: sitemap could not be fetched or parsed"


@final
class SitemapReader:
    """Read the pages a sitemap lists via ultimate-sitemap-parser (USP).

    Every fetch goes through :class:`GatedSitemapWebClient`, so each hop passes
    the SSRF gate.  URLs reach the log only in redacted form (CWE-532).  ``read``
    raises for a sitemap that could not be fetched or parsed instead of reading
    as an empty crawl; ``discover`` treats an unreadable sitemap as absent.  Both
    turn a USP parser failure, whose message quotes the URL, into a URL-free
    :class:`FetchRefusedError`.
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

    def discover(self, homepage: HttpUrl) -> list[SitemapPage]:
        """Return every page found by sitemap discovery for *homepage*.

        Raises:
            FetchRefusedError: The parser rejected *homepage*.  An absent or
                unreadable sitemap is not a failure here: the tree is just empty.
        """
        from usp.tree import sitemap_tree_for_homepage  # noqa: PLC0415

        logger.info("Discovering sitemaps for %s", homepage)
        try:
            tree = sitemap_tree_for_homepage(
                homepage.fetch_url, web_client=self._client
            )
            return list(tree.all_pages())
        except _PARSER_FAILURES as exc:
            logger.debug("sitemap parser failed: %s", type(exc).__name__)
            raise FetchRefusedError(_PARSER_REFUSAL) from None

    def read(self, url: HttpUrl) -> list[SitemapPage]:
        """Return the pages of the sitemap at *url*, recursing into indexes.

        Raises:
            FetchRefusedError: The sitemap could not be fetched or parsed.  The
                message omits the URL, which USP's own reason embeds.
        """
        from usp.fetch_parse import SitemapFetcher  # noqa: PLC0415

        logger.info("Fetching sitemap: %s", url)
        try:
            sitemap = SitemapFetcher(
                url=url.fetch_url, recursion_level=0, web_client=self._client
            ).sitemap()
            pages = list(sitemap.all_pages())
        except _PARSER_FAILURES as exc:
            logger.debug("sitemap parser failed: %s", type(exc).__name__)
            raise FetchRefusedError(_PARSER_REFUSAL) from None
        failed = isinstance(sitemap, InvalidSitemap) or any(
            isinstance(node, InvalidSitemap) for node in sitemap.all_sitemaps()
        )
        if failed and not pages:
            raise FetchRefusedError(_UNREADABLE)
        logger.info("Parsed %d pages from %s", len(pages), url)
        return pages
