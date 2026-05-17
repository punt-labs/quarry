"""Tests for sitemap crawling: discover, filter, dedup, ingest."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock, patch

import numpy as np

from quarry.db import Database
from quarry.sitemap import (
    SitemapDiscovery,
    SitemapEntry,
)

# ---------------------------------------------------------------------------
# discover_pages via USP sitemap_tree_for_homepage
# ---------------------------------------------------------------------------


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
        mock_tree_fn.assert_called_once_with("https://example.com/")

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
        mock_fetcher_cls.assert_called_once_with(
            url="https://example.com/sitemap.xml", recursion_level=0
        )

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

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_skips_when_lastmod_older(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

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
            Database(MagicMock()),
            MagicMock(),
            collection="test",
        )

        assert result["skipped"] == 1
        assert result["ingested"] == 0
        mock_ingest.assert_not_called()

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_ingests_when_lastmod_newer(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

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
            Database(MagicMock()),
            MagicMock(),
            collection="test",
        )

        assert result["skipped"] == 0
        assert result["ingested"] == 1

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_ingests_when_no_existing_doc(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

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
            Database(MagicMock()),
            MagicMock(),
            collection="test",
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_overwrite_bypasses_dedup(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

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
            Database(MagicMock()),
            MagicMock(),
            collection="test",
            overwrite=True,
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_no_lastmod_always_ingests(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

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
            Database(MagicMock()),
            MagicMock(),
            collection="test",
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0


# ---------------------------------------------------------------------------
# Integration: full pipeline with mocked HTTP and embeddings
# ---------------------------------------------------------------------------


class TestIngestSitemapIntegration:
    """End-to-end integration following test_url_ingestion.py pattern."""

    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    @patch("quarry.ingestion.pipeline._fetch_url")
    def test_end_to_end(
        self,
        mock_fetch_url: MagicMock,
        mock_discover: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

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

        mock_lance = MagicMock()
        mock_lance.open_table.return_value = MagicMock()
        mock_lance.list_tables.return_value = MagicMock(tables=[])
        db = Database(mock_lance)

        with (
            patch(
                "quarry.ingestion.pipeline.get_embedding_backend",
            ) as mock_embed_factory,
            patch("quarry.db.chunk_store.ChunkStore.insert", return_value=1),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
        ):
            mock_backend = MagicMock()
            mock_backend.model_name = "test-model"
            mock_backend.embed_texts.return_value = np.zeros(
                (10, 768), dtype=np.float32
            )
            mock_embed_factory.return_value = mock_backend

            result = ingest_sitemap(
                "https://docs.example.com/sitemap.xml",
                db,
                settings,
                collection="docs",
            )

        assert result["sitemap_url"] == ("https://docs.example.com/sitemap.xml")
        assert result["collection"] == "docs"
        assert result["total_discovered"] == 2
        assert result["ingested"] == 2
        assert result["failed"] == 0
        assert result["errors"] == []

    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    @patch("quarry.ingestion.pipeline._fetch_url")
    def test_with_filters(
        self,
        mock_fetch_url: MagicMock,
        mock_discover: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/docs/api", lastmod=None),
            SitemapEntry(loc="https://example.com/docs/guide", lastmod=None),
            SitemapEntry(loc="https://example.com/blog/post", lastmod=None),
        ]
        mock_fetch_url.return_value = "<html><body><p>Content.</p></body></html>"

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200

        mock_lance = MagicMock()
        mock_lance.open_table.return_value = MagicMock()
        mock_lance.list_tables.return_value = MagicMock(tables=[])
        db = Database(mock_lance)

        with (
            patch(
                "quarry.ingestion.pipeline.get_embedding_backend",
            ) as mock_embed_factory,
            patch("quarry.db.chunk_store.ChunkStore.insert", return_value=1),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
        ):
            mock_backend = MagicMock()
            mock_backend.model_name = "test-model"
            mock_backend.embed_texts.return_value = np.zeros(
                (10, 768), dtype=np.float32
            )
            mock_embed_factory.return_value = mock_backend

            result = ingest_sitemap(
                "https://example.com/sitemap.xml",
                db,
                settings,
                include=["/docs/*"],
                exclude=[],
            )

        assert result["total_discovered"] == 3
        assert result["after_filter"] == 2
        assert result["ingested"] == 2

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_default_collection_from_domain(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        _mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

        mock_discover.return_value = []
        mock_list_docs.return_value = []

        result = ingest_sitemap(
            "https://docs.python.org/sitemap.xml",
            Database(MagicMock()),
            MagicMock(),
        )

        assert result["collection"] == "docs.python.org"

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.db.chunk_catalog.ChunkCatalog.list_documents")
    @patch("quarry.sitemap.SitemapDiscovery.discover_urls")
    def test_handles_ingest_failure(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_sitemap

        mock_discover.return_value = [
            SitemapEntry(loc="https://example.com/good", lastmod=None),
            SitemapEntry(loc="https://example.com/bad", lastmod=None),
        ]
        mock_list_docs.return_value = []

        def _side_effect(
            url: str, *args: object, **kwargs: object
        ) -> dict[str, object]:
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
            Database(MagicMock()),
            MagicMock(),
            collection="test",
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

    @patch("quarry.ingestion.pipeline._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_routes_to_bulk_ingest_when_pages_discovered(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

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
            Database(MagicMock()),
            MagicMock(),
        )

        assert "sitemap_url" in result
        assert result["ingested"] == 2  # type: ignore[typeddict-item]
        # Pre-filtering is applied before _bulk_ingest_entries; entries
        # are already filtered so include is not passed through.
        call_kwargs = mock_bulk.call_args
        entries_arg = call_kwargs.args[0]
        assert len(entries_arg) == 2
        assert all(e.loc.startswith("https://example.com/docs") for e in entries_arg)

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_falls_back_to_single_page(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

        mock_discover.return_value = []
        mock_ingest_url.return_value = {
            "document_name": "https://example.com/page",
            "collection": "example.com",
            "chunks": 3,
        }

        result = ingest_auto(
            "https://example.com/page",
            Database(MagicMock()),
            MagicMock(),
        )

        assert "document_name" in result
        assert result["chunks"] == 3  # type: ignore[typeddict-item]
        mock_ingest_url.assert_called_once()

    @patch("quarry.ingestion.pipeline._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_no_path_filter_for_root_url(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

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

        ingest_auto("https://example.com/", Database(MagicMock()), MagicMock())

        call_kwargs = mock_bulk.call_args
        # Root URL has no path filter; include is not passed (defaults to None)
        assert call_kwargs.kwargs.get("include") is None

    @patch("quarry.ingestion.pipeline._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_collection_defaults_to_hostname(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

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
            "https://docs.python.org/3/library/", Database(MagicMock()), MagicMock()
        )

        call_kwargs = mock_bulk.call_args
        assert call_kwargs.kwargs["collection"] == "docs.python.org"

    @patch("quarry.ingestion.pipeline._bulk_ingest_entries")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_explicit_collection_passed_through(
        self,
        mock_discover: MagicMock,
        mock_bulk: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

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
            Database(MagicMock()),
            MagicMock(),
            collection="my-docs",
        )

        call_kwargs = mock_bulk.call_args
        assert call_kwargs.kwargs["collection"] == "my-docs"

    @patch("quarry.ingestion.pipeline.ingest_sitemap")
    def test_explicit_sitemap_url_skips_discovery(
        self,
        mock_ingest_sitemap: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

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
            Database(MagicMock()),
            MagicMock(),
        )

        assert "sitemap_url" in result
        mock_ingest_sitemap.assert_called_once()

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_discovery_error_falls_back_to_single_page(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        from quarry.ingestion.pipeline import ingest_auto

        mock_discover.side_effect = ConnectionError("network error")
        mock_ingest_url.return_value = {
            "document_name": "https://example.com/page",
            "collection": "example.com",
            "chunks": 3,
        }

        result = ingest_auto(
            "https://example.com/page",
            Database(MagicMock()),
            MagicMock(),
        )

        assert "document_name" in result
        mock_ingest_url.assert_called_once()

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_explicit_collection_passed_through_single_page_fallback(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """Explicit collection is preserved when falling back to single-page ingest."""
        from quarry.ingestion.pipeline import ingest_auto

        mock_discover.return_value = []
        mock_ingest_url.return_value = {
            "document_name": "https://example.com/page",
            "collection": "my-custom-collection",
            "chunks": 5,
        }

        ingest_auto(
            "https://example.com/page",
            Database(MagicMock()),
            MagicMock(),
            collection="my-custom-collection",
        )

        call_kwargs = mock_ingest_url.call_args
        assert call_kwargs.kwargs["collection"] == "my-custom-collection"

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_explicit_collection_passed_through_filter_zero_fallback(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """Explicit collection is preserved when sitemap filter yields zero pages."""
        from quarry.ingestion.pipeline import ingest_auto

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
            Database(MagicMock()),
            MagicMock(),
            collection="docker-sandboxes",
        )

        call_kwargs = mock_ingest_url.call_args
        assert call_kwargs.kwargs["collection"] == "docker-sandboxes"

    @patch("quarry.ingestion.pipeline.ingest_sitemap")
    def test_explicit_collection_passed_through_sitemap_url(
        self,
        mock_ingest_sitemap: MagicMock,
    ) -> None:
        """Explicit collection is preserved when URL is detected as a sitemap."""
        from quarry.ingestion.pipeline import ingest_auto

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
            Database(MagicMock()),
            MagicMock(),
            collection="my-docs",
        )

        call_kwargs = mock_ingest_sitemap.call_args
        assert call_kwargs.kwargs["collection"] == "my-docs"

    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_sitemap_substring_not_misdetected(
        self,
        mock_discover: MagicMock,
    ) -> None:
        """A URL like /docs/sitemap-guide should NOT be treated as a sitemap."""
        from quarry.ingestion.pipeline import ingest_auto

        mock_discover.return_value = []

        with patch("quarry.ingestion.pipeline.ingest_url") as mock_ingest_url:
            mock_ingest_url.return_value = {
                "document_name": "https://example.com/docs/sitemap-guide",
                "collection": "example.com",
                "chunks": 2,
            }
            result = ingest_auto(
                "https://example.com/docs/sitemap-guide",
                Database(MagicMock()),
                MagicMock(),
            )

        # Should fall through to single-page, not route to ingest_sitemap
        assert "document_name" in result

    @patch("quarry.ingestion.pipeline.ingest_url")
    @patch("quarry.sitemap.SitemapDiscovery.discover_pages")
    def test_falls_back_to_single_page_when_filter_yields_zero(
        self,
        mock_discover: MagicMock,
        mock_ingest_url: MagicMock,
    ) -> None:
        """Sitemap found but no pages match the requested path — fall back."""
        from quarry.ingestion.pipeline import ingest_auto

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
            Database(MagicMock()),
            MagicMock(),
        )

        assert "document_name" in result
        mock_ingest_url.assert_called_once()
