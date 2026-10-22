"""Tests for quarry.ingestion.sitemap_reader: gated, quiet, loud-on-failure reads."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from usp.exceptions import SitemapException
from usp.objects.page import SitemapPage
from usp.objects.sitemap import (
    IndexXMLSitemap,
    InvalidSitemap,
    PagesXMLSitemap,
)

from quarry.http_url import HttpUrl
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.sitemap_reader import SitemapReader

_URL = "https://example.com/sitemap.xml?token=SECRET"
_PARSER_REFUSAL = "fetch failed: sitemap URL not accepted by the parser"
_UNREADABLE = "fetch failed: sitemap could not be fetched or parsed"
_FETCHER = "usp.fetch_parse.SitemapFetcher"
_TREE = "usp.tree.sitemap_tree_for_homepage"


def _pages(*urls: str) -> PagesXMLSitemap:
    return PagesXMLSitemap(
        url="https://example.com/s.xml", pages=[SitemapPage(url=u) for u in urls]
    )


def _invalid() -> InvalidSitemap:
    return InvalidSitemap(url="https://example.com/x.xml", reason="HTTP 500")


class TestRead:
    @patch(_FETCHER)
    def test_the_pages_of_a_sitemap_are_returned(self, fetcher: MagicMock) -> None:
        fetcher.return_value.sitemap.return_value = _pages("https://a.test/1")
        pages = SitemapReader().read(HttpUrl(_URL))
        assert [p.url for p in pages] == ["https://a.test/1"]

    @patch(_FETCHER)
    def test_the_injected_client_does_the_fetching(self, fetcher: MagicMock) -> None:
        fetcher.return_value.sitemap.return_value = _pages()
        client = MagicMock()
        SitemapReader(client).read(HttpUrl(_URL))
        assert fetcher.call_args.kwargs["web_client"] is client

    @patch(_FETCHER)
    def test_an_invalid_sitemap_raises_without_the_url(
        self, fetcher: MagicMock
    ) -> None:
        fetcher.return_value.sitemap.return_value = InvalidSitemap(
            url=_URL, reason=f"HTTP 500 for {_URL}"
        )
        with pytest.raises(FetchRefusedError) as info:
            SitemapReader().read(HttpUrl(_URL))
        assert str(info.value) == _UNREADABLE

    @patch(_FETCHER)
    def test_an_index_whose_sub_sitemaps_all_failed_raises(
        self, fetcher: MagicMock
    ) -> None:
        index = IndexXMLSitemap(
            url="https://example.com/index.xml", sub_sitemaps=[_invalid(), _invalid()]
        )
        fetcher.return_value.sitemap.return_value = index
        with pytest.raises(FetchRefusedError) as info:
            SitemapReader().read(HttpUrl(_URL))
        assert str(info.value) == _UNREADABLE

    @patch(_FETCHER)
    def test_an_index_with_one_failed_sub_sitemap_keeps_the_rest(
        self, fetcher: MagicMock
    ) -> None:
        index = IndexXMLSitemap(
            url="https://example.com/index.xml",
            sub_sitemaps=[_invalid(), _pages("https://a.test/1")],
        )
        fetcher.return_value.sitemap.return_value = index
        assert [p.url for p in SitemapReader().read(HttpUrl(_URL))] == [
            "https://a.test/1"
        ]

    @patch(_FETCHER)
    def test_a_healthy_empty_sitemap_is_not_a_failure(self, fetcher: MagicMock) -> None:
        fetcher.return_value.sitemap.return_value = _pages()
        assert SitemapReader().read(HttpUrl(_URL)) == []

    @pytest.mark.parametrize(
        "failure",
        [
            SitemapException("URL https://h/s.xml?token=SECRET is not a HTTP(s) URL."),
            ValueError("bad https://h/s.xml?token=SECRET"),
            OSError("refused https://h/s.xml?token=SECRET"),
        ],
        ids=["sitemap", "value", "os"],
    )
    @patch(_FETCHER)
    def test_a_parser_failure_raises_without_the_url_or_its_cause(
        self, fetcher: MagicMock, failure: Exception
    ) -> None:
        fetcher.side_effect = failure
        with pytest.raises(FetchRefusedError) as info:
            SitemapReader().read(HttpUrl(_URL))
        assert str(info.value) == _PARSER_REFUSAL
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__

    @patch(_FETCHER)
    def test_a_programming_error_is_not_swallowed(self, fetcher: MagicMock) -> None:
        fetcher.side_effect = TypeError("a bug")
        with pytest.raises(TypeError):
            SitemapReader().read(HttpUrl(_URL))

    @patch(_FETCHER)
    def test_only_the_redacted_url_is_logged(
        self, fetcher: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        fetcher.return_value.sitemap.return_value = _pages("https://a.test/1")
        with caplog.at_level(logging.INFO, logger="quarry.ingestion.sitemap_reader"):
            SitemapReader().read(HttpUrl(_URL))
        assert [r.getMessage() for r in caplog.records] == [
            "Fetching sitemap: https://example.com/sitemap.xml",
            "Parsed 1 pages from https://example.com/sitemap.xml",
        ]


class TestDiscover:
    @patch(_TREE)
    def test_the_pages_of_the_tree_are_returned(self, tree: MagicMock) -> None:
        tree.return_value = _pages("https://a.test/1")
        assert [p.url for p in SitemapReader().discover(HttpUrl(_URL))] == [
            "https://a.test/1"
        ]

    @patch(_TREE, side_effect=SitemapException("URL x?token=SECRET is bad"))
    def test_a_parser_failure_raises_without_the_url(self, _tree: MagicMock) -> None:
        with pytest.raises(FetchRefusedError) as info:
            SitemapReader().discover(HttpUrl(_URL))
        assert str(info.value) == _PARSER_REFUSAL
        assert info.value.__cause__ is None

    @patch(_TREE)
    def test_only_the_redacted_homepage_is_logged(
        self, tree: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        tree.return_value = _pages()
        with caplog.at_level(logging.INFO, logger="quarry.ingestion.sitemap_reader"):
            SitemapReader().discover(HttpUrl(_URL))
        assert [r.getMessage() for r in caplog.records] == [
            "Discovering sitemaps for https://example.com/sitemap.xml"
        ]


def test_shown_never_raises_on_a_bad_port() -> None:
    assert SitemapReader.shown("http://u:s@h:99999/x?t=1") == "(unparseable URL)"
