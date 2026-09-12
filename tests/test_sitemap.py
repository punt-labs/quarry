"""Tests for sitemap crawling: discover, filter, dedup, ingest."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from quarry.db import Database
from quarry.ingestion.bulk_ingest import BulkOptions
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.sitemap import (
    SitemapDiscovery,
    SitemapEntry,
)
from quarry.sitemap_web_client import GatedSitemapWebClient

_GETADDRINFO = "quarry.url_safety.socket_module.getaddrinfo"


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
    """One getaddrinfo record resolving to *ip*."""
    family = 10 if ":" in ip else 2
    sockaddr: tuple[object, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, 1, 6, "", sockaddr)]


@pytest.fixture(autouse=True)
def _resolve_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public address by default.

    The sitemap ingest now SSRF-gates each entry (resolving its host), so an
    un-mocked test would hit real DNS or drop entries in CI; the SSRF tests
    below override this with host-specific internal addresses.
    """
    monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo("93.184.216.34"))


# ---------------------------------------------------------------------------
# discover_pages via USP sitemap_tree_for_homepage
# ---------------------------------------------------------------------------


class TestSitemapEntry:
    """SitemapEntry is a frozen, slotted value object."""

    def test_is_slotted(self) -> None:
        entry = SitemapEntry(loc="https://example.com/p", lastmod=None)
        assert SitemapEntry.__slots__ == ("loc", "lastmod")
        with pytest.raises(AttributeError):
            _ = entry.__dict__  # slotted instances have no __dict__


class TestDiscoverPages:
    """Test auto-discovery using USP's sitemap_tree_for_homepage."""

    @patch("usp.tree.sitemap_tree_for_homepage")
    def test_extracts_origin_and_discovers(self, mock_tree_fn: MagicMock) -> None:
        from usp.objects.page import SitemapPage

        mock_tree = MagicMock()
        mock_tree.all_pages.return_value = [
            SitemapPage(url="https://example.com/page1", last_modified=None),
            SitemapPage(url="https://example.com/page2", last_modified=None),
        ]
        mock_tree_fn.return_value = mock_tree

        entries = SitemapDiscovery.discover_pages("https://example.com/docs/guide")
        assert len(entries) == 2
        call = mock_tree_fn.call_args
        assert call.args == ("https://example.com/",)
        assert isinstance(call.kwargs["web_client"], GatedSitemapWebClient)

    @patch("usp.tree.sitemap_tree_for_homepage")
    def test_returns_empty_when_no_pages(self, mock_tree_fn: MagicMock) -> None:
        mock_tree = MagicMock()
        mock_tree.all_pages.return_value = []
        mock_tree_fn.return_value = mock_tree

        entries = SitemapDiscovery.discover_pages("https://example.com/")
        assert entries == []

    @patch("usp.tree.sitemap_tree_for_homepage")
    def test_preserves_lastmod(self, mock_tree_fn: MagicMock) -> None:
        from usp.objects.page import SitemapPage

        ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        mock_tree = MagicMock()
        mock_tree.all_pages.return_value = [
            SitemapPage(url="https://example.com/page", last_modified=ts),
        ]
        mock_tree_fn.return_value = mock_tree

        entries = SitemapDiscovery.discover_pages("https://example.com/")
        assert entries[0].lastmod == ts

    @patch("usp.tree.sitemap_tree_for_homepage")
    def test_deduplicates_by_url(self, mock_tree_fn: MagicMock) -> None:
        from usp.objects.page import SitemapPage

        mock_tree = MagicMock()
        mock_tree.all_pages.return_value = [
            SitemapPage(url="https://example.com/dup", last_modified=None),
            SitemapPage(url="https://example.com/dup", last_modified=None),
            SitemapPage(url="https://example.com/unique", last_modified=None),
        ]
        mock_tree_fn.return_value = mock_tree

        entries = SitemapDiscovery.discover_pages("https://example.com/")
        assert len(entries) == 2
        locs = [e.loc for e in entries]
        assert locs.count("https://example.com/dup") == 1


# ---------------------------------------------------------------------------
# discover_urls via USP SitemapFetcher
# ---------------------------------------------------------------------------


class TestDiscoverUrls:
    """Test explicit sitemap URL parsing via USP's SitemapFetcher."""

    @patch("usp.fetch_parse.SitemapFetcher")
    def test_returns_entries_from_sitemap(self, mock_fetcher_cls: MagicMock) -> None:
        from usp.objects.page import SitemapPage

        mock_sitemap = MagicMock()
        mock_sitemap.all_pages.return_value = [
            SitemapPage(url="https://example.com/page1", last_modified=None),
            SitemapPage(url="https://example.com/page2", last_modified=None),
        ]
        mock_fetcher = MagicMock()
        mock_fetcher.sitemap.return_value = mock_sitemap
        mock_fetcher_cls.return_value = mock_fetcher

        entries = SitemapDiscovery.discover_urls("https://example.com/sitemap.xml")
        assert len(entries) == 2
        assert entries[0].loc == "https://example.com/page1"
        call = mock_fetcher_cls.call_args
        assert call.kwargs["url"] == "https://example.com/sitemap.xml"
        assert call.kwargs["recursion_level"] == 0
        assert isinstance(call.kwargs["web_client"], GatedSitemapWebClient)

    @patch("usp.fetch_parse.SitemapFetcher")
    def test_deduplicates_pages(self, mock_fetcher_cls: MagicMock) -> None:
        from usp.objects.page import SitemapPage

        mock_sitemap = MagicMock()
        mock_sitemap.all_pages.return_value = [
            SitemapPage(url="https://example.com/shared", last_modified=None),
            SitemapPage(url="https://example.com/shared", last_modified=None),
            SitemapPage(url="https://example.com/unique", last_modified=None),
        ]
        mock_fetcher = MagicMock()
        mock_fetcher.sitemap.return_value = mock_sitemap
        mock_fetcher_cls.return_value = mock_fetcher

        entries = SitemapDiscovery.discover_urls("https://example.com/sitemap.xml")
        assert len(entries) == 2

    @patch("usp.fetch_parse.SitemapFetcher")
    def test_returns_empty_for_empty_sitemap(self, mock_fetcher_cls: MagicMock) -> None:
        mock_sitemap = MagicMock()
        mock_sitemap.all_pages.return_value = []
        mock_fetcher = MagicMock()
        mock_fetcher.sitemap.return_value = mock_sitemap
        mock_fetcher_cls.return_value = mock_fetcher

        entries = SitemapDiscovery.discover_urls("https://example.com/sitemap.xml")
        assert entries == []


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


_FILTER_ENTRIES: list[SitemapEntry] = [
    SitemapEntry(loc="https://example.com/docs/api", lastmod=None),
    SitemapEntry(loc="https://example.com/docs/guide", lastmod=None),
    SitemapEntry(loc="https://example.com/blog/post1", lastmod=None),
    SitemapEntry(loc="https://example.com/docs/v1/old", lastmod=None),
]


class TestFilterEntries:
    """Test include/exclude glob and limit filtering."""

    _entries: ClassVar[list[SitemapEntry]] = _FILTER_ENTRIES

    def test_include_matches_path(self) -> None:
        result = SitemapDiscovery.filter_entries(self._entries, include=["/docs/*"])
        locs = [e.loc for e in result]
        assert "https://example.com/docs/api" in locs
        assert "https://example.com/docs/guide" in locs
        assert "https://example.com/blog/post1" not in locs

    def test_exclude_removes_matching(self) -> None:
        result = SitemapDiscovery.filter_entries(self._entries, exclude=["/blog/*"])
        locs = [e.loc for e in result]
        assert "https://example.com/blog/post1" not in locs
        assert len(locs) == 3

    def test_exclude_takes_precedence(self) -> None:
        result = SitemapDiscovery.filter_entries(
            self._entries,
            include=["/docs/*"],
            exclude=["/docs/v1/*"],
        )
        locs = [e.loc for e in result]
        assert "https://example.com/docs/api" in locs
        assert "https://example.com/docs/v1/old" not in locs

    def test_limit_caps_results(self) -> None:
        result = SitemapDiscovery.filter_entries(self._entries, limit=2)
        assert len(result) == 2

    def test_combined_include_exclude_limit(self) -> None:
        result = SitemapDiscovery.filter_entries(
            self._entries,
            include=["/docs/*"],
            exclude=["/docs/v1/*"],
            limit=1,
        )
        assert len(result) == 1
        assert result[0].loc == "https://example.com/docs/api"

    def test_no_filters_returns_all(self) -> None:
        result = SitemapDiscovery.filter_entries(self._entries)
        assert len(result) == len(self._entries)


# ---------------------------------------------------------------------------
# Dedup (lastmod vs ingestion_timestamp) via full pipeline
# ---------------------------------------------------------------------------

_MOCK_RESULT: dict[str, object] = {
    "document_name": "p",
    "collection": "c",
    "chunks": 1,
}


class TestIngestSitemapDedup:
    """Test lastmod-based deduplication in ingest_sitemap."""

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_skips_when_lastmod_older(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(
                loc="https://example.com/page1",
                lastmod=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ]
        mock_list_docs.return_value = [
            {
                "document_name": "https://example.com/page1",
                "ingestion_timestamp": "2025-06-01T00:00:00+00:00",
            }
        ]

        result = ingest_sitemap(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["skipped"] == 1
        assert result["ingested"] == 0
        mock_ingest.assert_not_called()

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_ingests_when_lastmod_newer(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(
                loc="https://example.com/page1",
                lastmod=datetime(2025, 12, 1, tzinfo=UTC),
            ),
        ]
        mock_list_docs.return_value = [
            {
                "document_name": "https://example.com/page1",
                "ingestion_timestamp": "2025-01-01T00:00:00+00:00",
            }
        ]
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["skipped"] == 0
        assert result["ingested"] == 1

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_ingests_when_no_existing_doc(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(
                loc="https://example.com/new-page",
                lastmod=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ]
        mock_list_docs.return_value = []
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_overwrite_bypasses_dedup(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(
                loc="https://example.com/page1",
                lastmod=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ]
        mock_list_docs.return_value = [
            {
                "document_name": "https://example.com/page1",
                "ingestion_timestamp": "2025-06-01T00:00:00+00:00",
            }
        ]
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(
                Database(MagicMock()), MagicMock(), overwrite=True, collection="test"
            ),
            BulkOptions(),
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_no_lastmod_always_ingests(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/page1", lastmod=None),
        ]
        mock_list_docs.return_value = [
            {
                "document_name": "https://example.com/page1",
                "ingestion_timestamp": "2025-06-01T00:00:00+00:00",
            }
        ]
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0


# ---------------------------------------------------------------------------
# Integration: full pipeline with mocked HTTP and embeddings
# ---------------------------------------------------------------------------


class TestIngestSitemapIntegration:
    """End-to-end integration following test_url_ingestion.py pattern."""

    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    @patch("quarry.ingestion.web_fetch.WebFetcher.fetch")
    def test_end_to_end(
        self,
        mock_fetch_url: MagicMock,
        mock_discover: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(loc="https://docs.example.com/intro", lastmod=None),
            SitemapEntry(loc="https://docs.example.com/setup", lastmod=None),
        ]
        mock_fetch_url.return_value = (
            "<html><head><title>Docs</title></head>"
            "<body><h1>Getting Started</h1>"
            "<p>Welcome to our documentation.</p>"
            "</body></html>"
        )

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200
        settings.sync_flush_mb = 32
        settings.embed_window_chunks = 512

        mock_lance = MagicMock()
        mock_lance.open_table.return_value = MagicMock()
        mock_lance.list_tables.return_value = MagicMock(tables=[])
        db = Database(mock_lance)

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
        ):
            result = ingest_sitemap(
                "https://docs.example.com/sitemap.xml",
                Progress(None),
                IngestContext(db, settings, collection="docs"),
                BulkOptions(),
            )

        assert result["sitemap_url"] == ("https://docs.example.com/sitemap.xml")
        assert result["collection"] == "docs"
        assert result["total_discovered"] == 2
        assert result["ingested"] == 2
        assert result["failed"] == 0
        assert result["errors"] == []

    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    @patch("quarry.ingestion.web_fetch.WebFetcher.fetch")
    def test_with_filters(
        self,
        mock_fetch_url: MagicMock,
        mock_discover: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/docs/api", lastmod=None),
            SitemapEntry(loc="https://example.com/docs/guide", lastmod=None),
            SitemapEntry(loc="https://example.com/blog/post", lastmod=None),
        ]
        mock_fetch_url.return_value = "<html><body><p>Content.</p></body></html>"

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200
        settings.sync_flush_mb = 32
        settings.embed_window_chunks = 512

        mock_lance = MagicMock()
        mock_lance.open_table.return_value = MagicMock()
        mock_lance.list_tables.return_value = MagicMock(tables=[])
        db = Database(mock_lance)

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
        ):
            result = ingest_sitemap(
                "https://example.com/sitemap.xml",
                Progress(None),
                IngestContext(db, settings),
                BulkOptions(include=["/docs/*"], exclude=[]),
            )

        assert result["total_discovered"] == 3
        assert result["after_filter"] == 2
        assert result["ingested"] == 2

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_default_collection_from_domain(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        _mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = []
        mock_list_docs.return_value = []

        result = ingest_sitemap(
            "https://docs.python.org/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        assert result["collection"] == "docs.python.org"

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_bare_context_default_collection_stays_default(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        _mock_ingest: MagicMock,
    ) -> None:
        """A caller who never touches ``collection`` gets IngestContext's own
        dataclass default ("default") -- the SAME resolved name the daemon
        route already queued the job on (DES-042). Treating "default" as a
        second "let the pipeline decide" sentinel and re-deriving the
        hostname here would write to a different table than the queue
        serialized on -- a routing violation. Only a genuinely empty
        collection (see test_default_collection_from_domain) derives the
        hostname.
        """
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = []
        mock_list_docs.return_value = []

        result = ingest_sitemap(
            "https://docs.python.org/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock()),
            BulkOptions(),
        )

        assert result["collection"] == "default"

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_explicit_default_collection_stays_default(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        _mock_ingest: MagicMock,
    ) -> None:
        """A caller who explicitly asks for ``--collection default`` gets
        exactly that table -- not a hostname-derived one -- so the write
        target matches the "default" queue key the daemon route already
        resolved and serialized on (DES-042).
        """
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = []
        mock_list_docs.return_value = []

        result = ingest_sitemap(
            "https://docs.python.org/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="default"),
            BulkOptions(),
        )

        assert result["collection"] == "default"

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_fetching_progress_redacts_url_secrets(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        _mock_ingest: MagicMock,
    ) -> None:
        """ingest_sitemap's own "Fetching sitemap: %s" progress line must not
        leak URL secrets either (CWE-532) -- it fires before any failure path.
        """
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = []
        mock_list_docs.return_value = []
        credentialed_url = "https://user:pass@docs.python.org/sitemap.xml?token=abc123"
        messages: list[str] = []

        ingest_sitemap(
            credentialed_url,
            Progress(messages.append),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert messages
        for message in messages:
            assert "pass" not in message
            assert "token=abc123" not in message
        # Exact match, not a host/path substring check (CodeQL
        # py/incomplete-url-substring-sanitization): pins the whole
        # redacted message rather than a fragment that could also match an
        # unrelated look-alike string.
        fetching = [m for m in messages if m.startswith("Fetching sitemap:")]
        assert fetching == ["Fetching sitemap: https://docs.python.org/sitemap.xml"]

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_handles_ingest_failure(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/good", lastmod=None),
            SitemapEntry(loc="https://example.com/bad", lastmod=None),
        ]
        mock_list_docs.return_value = []

        def _side_effect(
            request: object, *args: object, **kwargs: object
        ) -> dict[str, object]:
            url = request.url  # type: ignore[attr-defined]
            if "bad" in url:
                msg = "HTTP 500"
                raise ValueError(msg)
            return {
                "document_name": url,
                "collection": "test",
                "chunks": 1,
            }

        mock_ingest.side_effect = _side_effect

        result = ingest_sitemap(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["ingested"] == 1
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert "bad" in result["errors"][0]


# ---------------------------------------------------------------------------
# ingest_auto — discovery and routing
# ---------------------------------------------------------------------------


class TestIngestAuto:
    """Test smart URL ingestion with sitemap auto-discovery."""

    @patch("quarry.ingestion.sitemap_ingest.SitemapIngest._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_routes_to_bulk_ingest_when_pages_discovered(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/docs/a", lastmod=None),
            SitemapEntry(loc="https://example.com/docs/b", lastmod=None),
        ]
        mock_bulk.return_value = {
            "sitemap_url": "https://example.com/docs",
            "collection": "example.com",
            "total_discovered": 2,
            "after_filter": 2,
            "ingested": 2,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        result = ingest_auto(
            "https://example.com/docs",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        assert "sitemap_url" in result
        assert result["ingested"] == 2  # type: ignore[typeddict-item]
        # Pre-filtering is applied before _bulk_ingest_entries; entries
        # are already filtered so include is not passed through.
        call_kwargs = mock_bulk.call_args
        entries_arg = call_kwargs.args[0].entries
        assert len(entries_arg) == 2
        assert all(e.loc.startswith("https://example.com/docs") for e in entries_arg)

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_falls_back_to_single_page(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = []
        mock_ingest_url.return_value = {
            "document_name": "https://example.com/page",
            "collection": "example.com",
            "chunks": 3,
        }

        result = ingest_auto(
            "https://example.com/page",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        assert "document_name" in result
        assert result["chunks"] == 3  # type: ignore[typeddict-item]
        mock_ingest_url.assert_called_once()

    @patch("quarry.ingestion.sitemap_ingest.SitemapIngest._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_no_path_filter_for_root_url(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/page1", lastmod=None),
        ]
        mock_bulk.return_value = {
            "sitemap_url": "https://example.com/",
            "collection": "example.com",
            "total_discovered": 1,
            "after_filter": 1,
            "ingested": 1,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        ingest_auto(
            "https://example.com/",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        call_kwargs = mock_bulk.call_args
        # Root URL has no path filter: the discovered entry reaches
        # _bulk_ingest_entries unfiltered (ingest_auto only glob-filters when
        # the input URL has a non-root path).
        crawl = call_kwargs.args[0]
        assert len(crawl.entries) == 1
        assert crawl.entries[0].loc == "https://example.com/page1"

    @patch("quarry.ingestion.sitemap_ingest.SitemapIngest._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_collection_defaults_to_hostname(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = [
            SitemapEntry(loc="https://docs.python.org/3/library/os", lastmod=None),
        ]
        mock_bulk.return_value = {
            "sitemap_url": "https://docs.python.org/3/library/",
            "collection": "docs.python.org",
            "total_discovered": 0,
            "after_filter": 0,
            "ingested": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        ingest_auto(
            "https://docs.python.org/3/library/",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        call_kwargs = mock_bulk.call_args
        assert call_kwargs.args[2].collection == "docs.python.org"

    @patch("quarry.ingestion.sitemap_ingest.SitemapIngest._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_explicit_collection_passed_through(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/docs/a", lastmod=None),
        ]
        mock_bulk.return_value = {
            "sitemap_url": "https://example.com/docs",
            "collection": "my-docs",
            "total_discovered": 0,
            "after_filter": 0,
            "ingested": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        ingest_auto(
            "https://example.com/docs",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="my-docs"),
            BulkOptions(),
        )

        call_kwargs = mock_bulk.call_args
        assert call_kwargs.args[2].collection == "my-docs"

    @patch("quarry.ingestion.sitemap_ingest.SitemapIngest.ingest_sitemap")
    def test_explicit_sitemap_url_skips_discovery(
        self,
        mock_ingest_sitemap: MagicMock,
    ) -> None:
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_ingest_sitemap.return_value = {
            "sitemap_url": "https://example.com/sitemap.xml",
            "collection": "example.com",
            "total_discovered": 10,
            "after_filter": 10,
            "ingested": 10,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        result = ingest_auto(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        assert "sitemap_url" in result
        mock_ingest_sitemap.assert_called_once()

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_discovery_error_falls_back_to_single_page(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """USP's own documented failure modes fall back to a single-page ingest.

        GatedSitemapWebClient never lets a network error escape as an
        exception (it reports a non-retryable WebClientErrorResponse so USP
        skips the URL instead) -- so the "expected" exceptions this fallback
        catches are USP's own discovery/parse failures, not raw network
        errors.
        """
        from usp.exceptions import SitemapException

        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.side_effect = SitemapException("malformed homepage URL")
        mock_ingest_url.return_value = {
            "document_name": "https://example.com/page",
            "collection": "example.com",
            "chunks": 3,
        }

        result = ingest_auto(
            "https://example.com/page",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        assert "document_name" in result
        mock_ingest_url.assert_called_once()

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_discovery_error_redacts_url_secrets(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A discovery failure must never leak URL secrets into quarry.log or
        the progress stream (CWE-532).

        usp's own exception messages embed the raw URL verbatim (e.g. "URL
        {url} is not a HTTP(s) URL") -- a userinfo/query secret on the
        crawled URL itself must not reach either sink, regardless of what
        the exception message says.
        """
        from usp.exceptions import SitemapException

        from quarry.ingestion.sitemap_ingest import ingest_auto

        credentialed_url = "https://user:pass@example.com/page?token=abc123"
        mock_discover.side_effect = SitemapException(
            f"URL {credentialed_url} is not a HTTP(s) URL."
        )
        mock_ingest_url.return_value = {
            "document_name": credentialed_url,
            "collection": "example.com",
            "chunks": 1,
        }
        messages: list[str] = []

        with caplog.at_level(logging.WARNING):
            ingest_auto(
                credentialed_url,
                Progress(messages.append),
                IngestContext(Database(MagicMock()), MagicMock(), collection=""),
                BulkOptions(),
            )

        assert "user:pass" not in caplog.text
        assert "token=abc123" not in caplog.text
        assert not any("user:pass" in m or "token=abc123" in m for m in messages)
        # Exact match, not a host substring check (CodeQL
        # py/incomplete-url-substring-sanitization): pins the whole log
        # record rather than a fragment that could also match an unrelated
        # look-alike string.
        warning_records = [
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        ]
        assert (
            "Sitemap discovery failed for https://example.com/page "
            "(SitemapException)" in warning_records
        )

    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_discovering_progress_redacts_netloc_userinfo(
        self, mock_discover: MagicMock
    ) -> None:
        """The "Discovering sitemaps for %s://%s" progress line derives its
        netloc from urlparse, which -- unlike CaptureUrl -- keeps userinfo
        ("user:pass@host"); it must be redacted before reaching progress
        (CWE-532). discover_pages returns no entries so the single-page
        fallback below never runs a real fetch.
        """
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = []
        credentialed_url = "https://user:pass@example.com/page"
        messages: list[str] = []

        with patch("quarry.ingestion.sitemap_ingest.ingest_url") as mock_ingest_url:
            mock_ingest_url.return_value = {
                "document_name": credentialed_url,
                "collection": "example.com",
                "chunks": 1,
            }
            ingest_auto(
                credentialed_url,
                Progress(messages.append),
                IngestContext(Database(MagicMock()), MagicMock(), collection=""),
                BulkOptions(),
            )

        discovering = [m for m in messages if m.startswith("Discovering sitemaps for")]
        assert discovering
        assert "user:pass" not in discovering[0]
        # Exact match, not a host substring check (CodeQL
        # py/incomplete-url-substring-sanitization): pins the whole
        # message rather than a fragment that could also match an
        # unrelated look-alike host.
        assert discovering == ["Discovering sitemaps for https://example.com"]

    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_unexpected_discovery_error_propagates(
        self, mock_discover: MagicMock
    ) -> None:
        """A programmer error during discovery must not be swallowed as
        "no sitemap found" -- only USP's own documented exception types are
        caught."""
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.side_effect = TypeError("boom")

        with pytest.raises(TypeError, match="boom"):
            ingest_auto(
                "https://example.com/page",
                Progress(None),
                IngestContext(Database(MagicMock()), MagicMock(), collection=""),
                BulkOptions(),
            )

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_explicit_collection_passed_through_single_page_fallback(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """Explicit collection is preserved when falling back to single-page ingest."""
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = []
        mock_ingest_url.return_value = {
            "document_name": "https://example.com/page",
            "collection": "my-custom-collection",
            "chunks": 5,
        }

        ingest_auto(
            "https://example.com/page",
            Progress(None),
            IngestContext(
                Database(MagicMock()), MagicMock(), collection="my-custom-collection"
            ),
            BulkOptions(),
        )

        call_kwargs = mock_ingest_url.call_args
        assert call_kwargs.args[2].collection == "my-custom-collection"

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_explicit_collection_passed_through_filter_zero_fallback(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """Explicit collection is preserved when sitemap filter yields zero pages."""
        from quarry.ingestion.sitemap_ingest import ingest_auto

        # Sitemap returns pages that don't match the requested path
        mock_discover.return_value = [
            SitemapEntry(loc="https://docs.example.com/other/a", lastmod=None),
        ]
        mock_ingest_url.return_value = {
            "document_name": "https://docs.example.com/ai/sandboxes/",
            "collection": "docker-sandboxes",
            "chunks": 3,
        }

        ingest_auto(
            "https://docs.example.com/ai/sandboxes/",
            Progress(None),
            IngestContext(
                Database(MagicMock()), MagicMock(), collection="docker-sandboxes"
            ),
            BulkOptions(),
        )

        call_kwargs = mock_ingest_url.call_args
        assert call_kwargs.args[2].collection == "docker-sandboxes"

    @patch("quarry.ingestion.sitemap_ingest.SitemapIngest.ingest_sitemap")
    def test_explicit_collection_passed_through_sitemap_url(
        self,
        mock_ingest_sitemap: MagicMock,
    ) -> None:
        """Explicit collection is preserved when URL is detected as a sitemap."""
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_ingest_sitemap.return_value = {
            "sitemap_url": "https://example.com/sitemap.xml",
            "collection": "my-docs",
            "total_discovered": 5,
            "after_filter": 5,
            "ingested": 5,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        ingest_auto(
            "https://example.com/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="my-docs"),
            BulkOptions(),
        )

        call_kwargs = mock_ingest_sitemap.call_args
        assert call_kwargs.args[2].collection == "my-docs"

    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_sitemap_substring_not_misdetected(
        self,
        mock_discover: MagicMock,
    ) -> None:
        """A URL like /docs/sitemap-guide should NOT be treated as a sitemap."""
        from quarry.ingestion.sitemap_ingest import ingest_auto

        mock_discover.return_value = []

        with patch("quarry.ingestion.sitemap_ingest.ingest_url") as mock_ingest_url:
            mock_ingest_url.return_value = {
                "document_name": "https://example.com/docs/sitemap-guide",
                "collection": "example.com",
                "chunks": 2,
            }
            result = ingest_auto(
                "https://example.com/docs/sitemap-guide",
                Progress(None),
                IngestContext(Database(MagicMock()), MagicMock(), collection=""),
                BulkOptions(),
            )

        # Should fall through to single-page, not route to ingest_sitemap
        assert "document_name" in result

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_falls_back_to_single_page_when_filter_yields_zero(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """Sitemap found but no pages match the requested path — fall back."""
        from quarry.ingestion.sitemap_ingest import ingest_auto

        # Sitemap returns pages that don't match the requested path
        mock_discover.return_value = [
            SitemapEntry(loc="https://docs.example.com/guide/a", lastmod=None),
            SitemapEntry(loc="https://docs.example.com/guide/b", lastmod=None),
        ]
        mock_ingest_url.return_value = {
            "document_name": "https://docs.example.com/ai/sandboxes/get-started/",
            "collection": "test",
            "chunks": 5,
        }

        result = ingest_auto(
            "https://docs.example.com/ai/sandboxes/get-started/",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection=""),
            BulkOptions(),
        )

        assert "document_name" in result
        mock_ingest_url.assert_called_once()


# ---------------------------------------------------------------------------
# SSRF gate — select_safe glob-filters + gates + caps sitemap entries
# ---------------------------------------------------------------------------


class TestSelectSafe:
    """SitemapDiscovery.select_safe drops entries resolving to internal IPs."""

    @pytest.mark.parametrize(
        "resolved",
        [
            "169.254.169.254",
            "127.0.0.1",
            "::1",
            "10.0.0.5",
            "192.168.1.1",
            "100.64.0.1",
        ],
    )
    def test_drops_entry_resolving_to_blocked_class(
        self, resolved: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry whose host RESOLVES to any blocked class is dropped."""
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(resolved))
        entries = [SitemapEntry(loc="https://listed.attacker.test/x", lastmod=None)]
        assert SitemapDiscovery.select_safe(entries) == []

    def test_drops_metadata_host_without_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A literal cloud-metadata IP is dropped by hostname, before any DNS."""

        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("getaddrinfo must not run for a metadata IP")

        monkeypatch.setattr(_GETADDRINFO, _boom)
        entries = [SitemapEntry(loc="http://169.254.169.254/latest/", lastmod=None)]
        assert SitemapDiscovery.select_safe(entries) == []

    def test_keeps_safe_and_drops_unsafe_in_mixed_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the internal-resolving entry is dropped; safe entries survive."""
        resolve = {"safe.example": "93.184.216.34", "internal.example": "10.0.0.9"}

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            return _addrinfo(resolve.get(host, "93.184.216.34"))

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        entries = [
            SitemapEntry(loc="https://safe.example/a", lastmod=None),
            SitemapEntry(loc="https://internal.example/secret", lastmod=None),
            SitemapEntry(loc="https://safe.example/b", lastmod=None),
        ]
        safe = SitemapDiscovery.select_safe(entries)
        locs = [e.loc for e in safe]
        assert locs == ["https://safe.example/a", "https://safe.example/b"]

    def test_logs_each_drop(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dropped entry is logged with its URL and reason."""
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo("127.0.0.1"))
        entries = [SitemapEntry(loc="https://loops.example/x", lastmod=None)]
        with caplog.at_level(logging.WARNING, logger="quarry.sitemap"):
            SitemapDiscovery.select_safe(entries)
        assert "https://loops.example/x" in caplog.text

    def test_limit_counts_safe_entries_not_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With an internal entry first, limit still yields `limit` SAFE pages.

        Regression: applying the limit before the gate would take the first
        `limit` entries then drop the unsafe ones, under-delivering.
        """
        resolve = {"internal.example": "10.0.0.9"}

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            return _addrinfo(resolve.get(host, "93.184.216.34"))

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        entries = [
            SitemapEntry(loc="https://internal.example/x", lastmod=None),
            SitemapEntry(loc="https://safe.example/a", lastmod=None),
            SitemapEntry(loc="https://safe.example/b", lastmod=None),
            SitemapEntry(loc="https://safe.example/c", lastmod=None),
        ]
        safe = SitemapDiscovery.select_safe(entries, limit=2)
        assert [e.loc for e in safe] == [
            "https://safe.example/a",
            "https://safe.example/b",
        ]

    def test_limit_bounds_resolution_and_gates_each_considered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate runs on every considered entry and stops once limit is filled.

        Only ~enough hosts to fill the limit are resolved -- a huge sitemap is
        not fully resolved -- and every entry looked at goes through the gate.
        """
        from urllib.parse import urlparse as _real_urlparse

        resolved: list[str] = []
        parsed: list[str] = []

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            resolved.append(host)
            return _addrinfo("93.184.216.34")

        def _counting_urlparse(url: str) -> object:
            parsed.append(url)
            return _real_urlparse(url)

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        monkeypatch.setattr("quarry.sitemap.urlparse", _counting_urlparse)
        entries = [
            SitemapEntry(loc=f"https://safe{i}.example/p", lastmod=None)
            for i in range(50)
        ]
        safe = SitemapDiscovery.select_safe(entries, limit=3)
        assert len(safe) == 3
        # One lazy pass: glob (urlparse) AND gate (resolution) run only ~limit
        # times, not across all 50 entries.
        assert resolved == ["safe0.example", "safe1.example", "safe2.example"]
        assert len(parsed) == 3

    def test_fewer_safe_than_limit_returns_all_no_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sitemap with fewer safe entries than the limit returns all safe ones."""
        resolve = {"internal.example": "127.0.0.1"}

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            return _addrinfo(resolve.get(host, "93.184.216.34"))

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        entries = [
            SitemapEntry(loc="https://safe.example/a", lastmod=None),
            SitemapEntry(loc="https://internal.example/x", lastmod=None),
        ]
        safe = SitemapDiscovery.select_safe(entries, limit=5)
        assert [e.loc for e in safe] == ["https://safe.example/a"]


class TestBulkIngestSsrfGate:
    """The gate runs inside _bulk_ingest_entries, so every ingest surface is covered.

    Both CLI (`quarry ingest`) and MCP ingest reach ``ingest_sitemap`` /
    ``ingest_auto`` in the daemon, which funnel through ``_bulk_ingest_entries``;
    gating there closes the sitemap bypass for both surfaces at one choke point.
    """

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_internal_entry_dropped_batch_continues(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A sitemap listing an internal URL fetches only the safe entries."""
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        resolve = {"safe.example": "93.184.216.34", "internal.example": "10.0.0.9"}

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            return _addrinfo(resolve.get(host, "93.184.216.34"))

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        mock_discover.return_value = [
            SitemapEntry(loc="https://safe.example/page", lastmod=None),
            SitemapEntry(loc="https://internal.example/secret", lastmod=None),
        ]
        mock_list_docs.return_value = []
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://safe.example/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["ingested"] == 1  # only the safe entry
        fetched = [call.args[0].url for call in mock_ingest.call_args_list]
        assert fetched == ["https://safe.example/page"]
        assert "https://internal.example/secret" not in fetched

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_limit_delivers_limit_safe_pages_despite_early_internal(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """limit=2 with an internal URL first ingests 2 SAFE pages, not 1.

        Reproduces the under-delivery bug (limit applied before the gate) and
        proves the fix: the internal URL is never fetched, and the limit counts
        safe pages.
        """
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        resolve = {"internal.example": "10.0.0.9"}

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            return _addrinfo(resolve.get(host, "93.184.216.34"))

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        mock_discover.return_value = [
            SitemapEntry(loc="https://internal.example/secret", lastmod=None),
            SitemapEntry(loc="https://safe.example/a", lastmod=None),
            SitemapEntry(loc="https://safe.example/b", lastmod=None),
            SitemapEntry(loc="https://safe.example/c", lastmod=None),
        ]
        mock_list_docs.return_value = []
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://safe.example/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(limit=2),
        )

        assert result["ingested"] == 2  # two SAFE pages, not limit-minus-internal
        fetched = [call.args[0].url for call in mock_ingest.call_args_list]
        assert fetched == ["https://safe.example/a", "https://safe.example/b"]
        assert "https://internal.example/secret" not in fetched

    @patch("quarry.ingestion.sitemap_ingest.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_after_filter_reports_post_gate_count(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The summary's after_filter reflects the post-gate set, not the raw total."""
        from quarry.ingestion.sitemap_ingest import ingest_sitemap

        resolve = {"internal.example": "10.0.0.9"}

        def _resolver(host: str, *_a: object, **_k: object) -> object:
            return _addrinfo(resolve.get(host, "93.184.216.34"))

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        mock_discover.return_value = [
            SitemapEntry(loc="https://safe.example/a", lastmod=None),
            SitemapEntry(loc="https://internal.example/secret", lastmod=None),
            SitemapEntry(loc="https://safe.example/b", lastmod=None),
        ]
        mock_list_docs.return_value = []
        mock_ingest.return_value = _MOCK_RESULT

        result = ingest_sitemap(
            "https://safe.example/sitemap.xml",
            Progress(None),
            IngestContext(Database(MagicMock()), MagicMock(), collection="test"),
            BulkOptions(),
        )

        assert result["total_discovered"] == 3
        assert result["after_filter"] == 2  # post-gate, not the pre-gate 3
        assert result["ingested"] == 2
