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

from quarry.failure_reason import FailureReason
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.sitemap_node_failure import NodeFailure
from quarry.reported_error import ReportedError
from quarry.sitemap_web_client import GatedSitemapWebClient

if TYPE_CHECKING:
    from usp.objects.page import SitemapPage

    from quarry.http_url import HttpUrl

logger = logging.getLogger(__name__)

# The parser's messages quote the URL verbatim, so each failure it can raise is
# replaced by a fixed refusal, never chained or re-raised; only the type is
# logged with its frames.  These are the parser's own exceptions; anything else
# (a ValueError, an OSError) is a programming error and propagates to the task
# terminal, which records only its class (internal error: <Class>).
_PARSER_FAILURES = (
    SitemapException,
    SitemapXMLParsingException,
    GunzipException,
    StripURLToHomepageException,
)
_PARSER_REFUSAL = "fetch failed: sitemap URL not accepted by the parser"
_UNREADABLE = "fetch failed: sitemap could not be fetched or parsed"


class UnexpectedSitemapError(ReportedError):
    """A sitemap fetch hit a bug, not a network failure or a document defect.

    Raised with the class name of the bug the client recorded.  It is no refusal,
    so the task terminal records ``internal error: <reported_as>`` and the crawl
    does not report a partial success.
    """


@final
class SitemapReader:
    """Read the pages a sitemap lists via ultimate-sitemap-parser (USP).

    Every fetch goes through :class:`GatedSitemapWebClient`, so each hop passes
    the SSRF gate.  URLs reach the log only in redacted form (CWE-532).  ``read``
    raises for a sitemap that could not be fetched or parsed instead of reading
    as an empty crawl; ``discover`` treats an unreadable sitemap as absent, but
    never a fetch that hit a bug (:class:`UnexpectedSitemapError`).  Both
    turn a USP parser failure, whose message quotes the URL, into a URL-free
    :class:`FetchRefusedError`.
    """

    __slots__ = ("_client",)

    _client: GatedSitemapWebClient

    def __new__(cls, client: GatedSitemapWebClient | None = None) -> Self:
        self = super().__new__(cls)
        self._client = client or GatedSitemapWebClient()
        return self

    def discover(self, homepage: HttpUrl) -> list[SitemapPage]:
        """Return every page found by sitemap discovery for *homepage*.

        Raises:
            FetchRefusedError: The parser rejected *homepage*.  An absent or
                unreadable sitemap is not a failure here: the tree is just empty.
            UnexpectedSitemapError: A fetch hit a bug; never treated as an absent
                sitemap.
        """
        from usp.tree import sitemap_tree_for_homepage  # noqa: PLC0415

        logger.info("Discovering sitemaps for %s", homepage)
        self._client.take_bug()
        try:
            tree = sitemap_tree_for_homepage(
                homepage.fetch_url, web_client=self._client
            )
            pages = list(tree.all_pages())
        except _PARSER_FAILURES as exc:
            logger.warning(
                "sitemap parser failed: %s\n%s",
                type(exc).__name__,
                FailureReason.frames(exc),
            )
            raise FetchRefusedError(_PARSER_REFUSAL) from None
        bug = self._client.take_bug()
        for failure in NodeFailure.in_tree(tree, bug=bug):
            failure.log(logger, speculative=True)
        if bug:
            raise UnexpectedSitemapError(bug)
        return pages

    def read(self, url: HttpUrl) -> list[SitemapPage]:
        """Return the pages of the sitemap at *url*, recursing into indexes.

        Raises:
            FetchRefusedError: The sitemap could not be fetched or parsed.  The
                message omits the URL, which USP's own reason embeds.
        """
        from usp.fetch_parse import SitemapFetcher  # noqa: PLC0415

        logger.info("Fetching sitemap: %s", url)
        self._client.take_bug()
        try:
            sitemap = SitemapFetcher(
                url=url.fetch_url, recursion_level=0, web_client=self._client
            ).sitemap()
            pages = list(sitemap.all_pages())
        except _PARSER_FAILURES as exc:
            logger.warning(
                "sitemap parser failed: %s\n%s",
                type(exc).__name__,
                FailureReason.frames(exc),
            )
            raise FetchRefusedError(_PARSER_REFUSAL) from None
        bug = self._client.take_bug()
        self._judge(NodeFailure.in_tree(sitemap, bug=bug), len(pages), url, bug)
        if not pages:
            logger.warning("sitemap lists no pages: %s", url)
        logger.info("Parsed %d pages from %s", len(pages), url)
        return pages

    @staticmethod
    def _judge(
        failures: list[NodeFailure], page_count: int, url: HttpUrl, bug: str
    ) -> None:
        """Log every failed node, and raise if the read cannot be trusted.

        Raises:
            UnexpectedSitemapError: The client recorded a bug, whatever pages survived.
            FetchRefusedError: Nodes failed and no page was read.
        """
        for failure in failures:
            failure.log(logger)
        if bug:
            raise UnexpectedSitemapError(bug)
        if failures and not page_count:
            cause = next((f.cause for f in failures if f.cause), "")
            raise FetchRefusedError(f"{_UNREADABLE}: {cause}" if cause else _UNREADABLE)
        if failures:
            logger.warning(
                "%d sub-sitemap(s) of %s could not be read; %d pages kept",
                len(failures),
                url,
                page_count,
            )
