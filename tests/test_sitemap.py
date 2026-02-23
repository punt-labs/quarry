"""Tests for sitemap crawling: parse, discover, filter, dedup, ingest."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from quarry.sitemap import (
    SitemapEntry,
    discover_urls,
    filter_entries,
    parse_sitemap,
)

# ---------------------------------------------------------------------------
# Sitemap XML parsing
# ---------------------------------------------------------------------------


class TestParseSitemap:
    """Test XML parsing of <urlset> and <sitemapindex> documents."""

    def test_parse_urlset_with_lastmod(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/page1</loc>
    <lastmod>2025-01-15T10:30:00+00:00</lastmod>
  </url>
  <url>
    <loc>https://example.com/page2</loc>
    <lastmod>2025-02-20</lastmod>
  </url>
</urlset>"""
        entries, children = parse_sitemap(xml)
        assert len(entries) == 2
        assert children == []
        assert entries[0].loc == "https://example.com/page1"
        assert entries[0].lastmod is not None
        assert entries[0].lastmod.year == 2025
        assert entries[0].lastmod.month == 1
        assert entries[1].loc == "https://example.com/page2"
        assert entries[1].lastmod is not None
        assert entries[1].lastmod.day == 20

    def test_parse_urlset_without_lastmod(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/no-date</loc></url>
</urlset>"""
        entries, _ = parse_sitemap(xml)
        assert len(entries) == 1
        assert entries[0].loc == "https://example.com/no-date"
        assert entries[0].lastmod is None

    def test_parse_sitemapindex(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>
</sitemapindex>"""
        entries, children = parse_sitemap(xml)
        assert entries == []
        assert len(children) == 2
        assert children[0] == "https://example.com/sitemap1.xml"

    def test_parse_without_namespace(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>https://example.com/no-ns</loc></url>
</urlset>"""
        entries, _ = parse_sitemap(xml)
        assert len(entries) == 1
        assert entries[0].loc == "https://example.com/no-ns"

    def test_parse_sitemapindex_without_namespace(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex>
  <sitemap><loc>https://example.com/child.xml</loc></sitemap>
</sitemapindex>"""
        entries, children = parse_sitemap(xml)
        assert entries == []
        assert len(children) == 1

    def test_unknown_root_raises(self) -> None:
        xml = "<html><body>Not a sitemap</body></html>"
        with pytest.raises(ValueError, match="Unknown sitemap root"):
            parse_sitemap(xml)

    def test_lastmod_with_z_suffix(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/z</loc>
    <lastmod>2025-03-01T12:00:00Z</lastmod>
  </url>
</urlset>"""
        entries, _ = parse_sitemap(xml)
        assert entries[0].lastmod is not None
        assert entries[0].lastmod.tzinfo is not None

    def test_skips_url_without_loc(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><lastmod>2025-01-01</lastmod></url>
  <url><loc>https://example.com/valid</loc></url>
</urlset>"""
        entries, _ = parse_sitemap(xml)
        assert len(entries) == 1
        assert entries[0].loc == "https://example.com/valid"


# ---------------------------------------------------------------------------
# discover_urls with recursive sitemap indexes
# ---------------------------------------------------------------------------


class TestDiscoverUrls:
    """Test recursive sitemap discovery with mocked HTTP."""

    @patch("quarry.sitemap.fetch_sitemap")
    def test_follows_sitemap_index(self, mock_fetch: MagicMock) -> None:
        index_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""
        child_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>"""
        mock_fetch.side_effect = [index_xml, child_xml]

        entries = discover_urls("https://example.com/sitemap.xml")
        assert len(entries) == 2
        assert entries[0].loc == "https://example.com/page1"

    @patch("quarry.sitemap.fetch_sitemap")
    def test_respects_max_depth(self, mock_fetch: MagicMock) -> None:
        index1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/index2.xml</loc></sitemap>
</sitemapindex>"""
        index2 = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/final.xml</loc></sitemap>
</sitemapindex>"""
        mock_fetch.side_effect = [index1, index2]

        # depth 0 (index1), depth 1 (index2), depth 2 (final) exceeds
        entries = discover_urls("https://example.com/sitemap.xml", max_depth=1)
        assert entries == []
        assert mock_fetch.call_count == 2

    @patch("quarry.sitemap.fetch_sitemap")
    def test_deduplicates_urls(self, mock_fetch: MagicMock) -> None:
        index_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/child1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/child2.xml</loc></sitemap>
</sitemapindex>"""
        child1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/shared</loc></url>
  <url><loc>https://example.com/only1</loc></url>
</urlset>"""
        child2 = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/shared</loc></url>
  <url><loc>https://example.com/only2</loc></url>
</urlset>"""
        mock_fetch.side_effect = [index_xml, child1, child2]

        entries = discover_urls("https://example.com/sitemap.xml")
        locs = [e.loc for e in entries]
        assert len(locs) == 3
        assert locs.count("https://example.com/shared") == 1

    @patch("quarry.sitemap.fetch_sitemap")
    def test_flat_urlset(self, mock_fetch: MagicMock) -> None:
        urlset_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""
        mock_fetch.return_value = urlset_xml

        entries = discover_urls("https://example.com/sitemap.xml")
        assert len(entries) == 2


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
        result = filter_entries(self._entries, include=["/docs/*"])
        locs = [e.loc for e in result]
        assert "https://example.com/docs/api" in locs
        assert "https://example.com/docs/guide" in locs
        assert "https://example.com/blog/post1" not in locs

    def test_exclude_removes_matching(self) -> None:
        result = filter_entries(self._entries, exclude=["/blog/*"])
        locs = [e.loc for e in result]
        assert "https://example.com/blog/post1" not in locs
        assert len(locs) == 3

    def test_exclude_takes_precedence(self) -> None:
        result = filter_entries(
            self._entries,
            include=["/docs/*"],
            exclude=["/docs/v1/*"],
        )
        locs = [e.loc for e in result]
        assert "https://example.com/docs/api" in locs
        assert "https://example.com/docs/v1/old" not in locs

    def test_limit_caps_results(self) -> None:
        result = filter_entries(self._entries, limit=2)
        assert len(result) == 2

    def test_combined_include_exclude_limit(self) -> None:
        result = filter_entries(
            self._entries,
            include=["/docs/*"],
            exclude=["/docs/v1/*"],
            limit=1,
        )
        assert len(result) == 1
        assert result[0].loc == "https://example.com/docs/api"

    def test_no_filters_returns_all(self) -> None:
        result = filter_entries(self._entries)
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

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_skips_when_lastmod_older(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

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
            MagicMock(),
            MagicMock(),
            collection="test",
        )

        assert result["skipped"] == 1
        assert result["ingested"] == 0
        mock_ingest.assert_not_called()

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_ingests_when_lastmod_newer(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

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
            MagicMock(),
            MagicMock(),
            collection="test",
        )

        assert result["skipped"] == 0
        assert result["ingested"] == 1

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_ingests_when_no_existing_doc(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

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
            MagicMock(),
            MagicMock(),
            collection="test",
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_overwrite_bypasses_dedup(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

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
            MagicMock(),
            MagicMock(),
            collection="test",
            overwrite=True,
        )

        assert result["ingested"] == 1
        assert result["skipped"] == 0

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_no_lastmod_always_ingests(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

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
            MagicMock(),
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

    @patch("quarry.sitemap.fetch_sitemap")
    @patch("quarry.pipeline._fetch_url")
    def test_end_to_end(
        self,
        mock_fetch_url: MagicMock,
        mock_fetch_sitemap: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

        sitemap_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://docs.example.com/intro</loc></url>
  <url><loc>https://docs.example.com/setup</loc></url>
</urlset>"""
        mock_fetch_sitemap.return_value = sitemap_xml
        mock_fetch_url.return_value = (
            "<html><head><title>Docs</title></head>"
            "<body><h1>Getting Started</h1>"
            "<p>Welcome to our documentation.</p>"
            "</body></html>"
        )

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200

        db = MagicMock()
        db.open_table.return_value = MagicMock()
        db.list_tables.return_value = MagicMock(tables=[])

        with (
            patch("quarry.pipeline.get_embedding_backend") as mock_embed_factory,
            patch("quarry.pipeline.insert_chunks", return_value=1),
            patch("quarry.pipeline.list_documents", return_value=[]),
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

    @patch("quarry.sitemap.fetch_sitemap")
    @patch("quarry.pipeline._fetch_url")
    def test_with_filters(
        self,
        mock_fetch_url: MagicMock,
        mock_fetch_sitemap: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

        sitemap_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/docs/api</loc></url>
  <url><loc>https://example.com/docs/guide</loc></url>
  <url><loc>https://example.com/blog/post</loc></url>
</urlset>"""
        mock_fetch_sitemap.return_value = sitemap_xml
        mock_fetch_url.return_value = "<html><body><p>Content.</p></body></html>"

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200

        db = MagicMock()
        db.open_table.return_value = MagicMock()
        db.list_tables.return_value = MagicMock(tables=[])

        with (
            patch("quarry.pipeline.get_embedding_backend") as mock_embed_factory,
            patch("quarry.pipeline.insert_chunks", return_value=1),
            patch("quarry.pipeline.list_documents", return_value=[]),
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

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_default_collection_from_domain(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        _mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

        mock_discover.return_value = []
        mock_list_docs.return_value = []

        result = ingest_sitemap(
            "https://docs.python.org/sitemap.xml",
            MagicMock(),
            MagicMock(),
        )

        assert result["collection"] == "docs.python.org"

    @patch("quarry.pipeline.ingest_url")
    @patch("quarry.pipeline.list_documents")
    @patch("quarry.sitemap.discover_urls")
    def test_handles_ingest_failure(
        self,
        mock_discover: MagicMock,
        mock_list_docs: MagicMock,
        mock_ingest: MagicMock,
    ) -> None:
        from quarry.pipeline import ingest_sitemap

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
            MagicMock(),
            MagicMock(),
            collection="test",
        )

        assert result["ingested"] == 1
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert "bad" in result["errors"][0]
