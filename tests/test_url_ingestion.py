"""Tests for URL ingestion: process HTML, chunk, embed, store."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import numpy as np

from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.routes.ingestion import IngestionRoutes
from quarry.db import Database
from quarry.extractors.html_extractor import HtmlExtractor
from quarry.models import PageType

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext

_FETCH = "quarry.ingestion.web_fetch.WebFetcher.fetch"


class TestProcessHtmlText:
    """Test the shared HTML-to-pages function used by both file and URL paths."""

    def test_basic_html(self):
        html = "<html><body><h1>Title</h1><p>Content here.</p></body></html>"
        pages = HtmlExtractor().extract_from_html(
            html, "test.html", "https://example.com"
        )
        assert len(pages) >= 1
        assert pages[0].document_name == "test.html"
        assert pages[0].document_path == "https://example.com"
        assert pages[0].page_type == PageType.SECTION

    def test_strips_boilerplate(self):
        html = (
            "<html><body>"
            "<nav>Menu</nav>"
            "<script>alert(1)</script>"
            "<main><h1>Article</h1><p>Real content.</p></main>"
            "<footer>Copyright</footer>"
            "</body></html>"
        )
        pages = HtmlExtractor().extract_from_html(html, "doc", "https://example.com")
        text = " ".join(p.text for p in pages)
        assert "Real content" in text
        assert "Menu" not in text
        assert "alert" not in text
        assert "Copyright" not in text

    def test_empty_html_returns_empty(self):
        html = "<html><body></body></html>"
        pages = HtmlExtractor().extract_from_html(html, "e", "u")
        assert pages == []

    def test_title_prepended_when_no_headings(self):
        html = (
            "<html><head><title>My Page</title></head>"
            "<body><p>Some text.</p></body></html>"
        )
        pages = HtmlExtractor().extract_from_html(html, "doc", "u")
        assert any("My Page" in p.text for p in pages)


class TestIngestUrl:
    """Integration test: fetch -> process -> chunk -> embed -> store."""

    @patch(_FETCH)
    def test_end_to_end(self, mock_fetch: MagicMock):
        from quarry.ingestion.pipeline import ingest_url

        mock_fetch.return_value = (
            "<html><head><title>Docs</title></head>"
            "<body><h1>API Reference</h1>"
            "<p>The authenticate endpoint accepts a JWT token.</p>"
            "</body></html>"
        )
        settings = _fake_settings()
        db = _fake_db()

        with (
            patch(
                "quarry.ingestion.streaming.get_embedding_backend",
            ) as mock_embed_factory,
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            mock_embed_factory.return_value = _fake_backend()
            result = ingest_url(
                "https://docs.example.com/api",
                db,
                settings,
                collection="docs",
            )

        assert result["document_name"] == "https://docs.example.com/api"
        assert result["collection"] == "docs"
        assert result["chunks"] >= 1
        mock_fetch.assert_called_once_with("https://docs.example.com/api")

    @patch(_FETCH)
    def test_custom_document_name(self, mock_fetch: MagicMock):
        from quarry.ingestion.pipeline import ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        with (
            patch(
                "quarry.ingestion.streaming.get_embedding_backend",
            ) as mock_embed_factory,
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            mock_embed_factory.return_value = _fake_backend()
            result = ingest_url(
                "https://example.com/page",
                db,
                settings,
                document_name="my-page",
            )

        assert result["document_name"] == "my-page"

    @patch(_FETCH)
    def test_capture_path_redacts_url_metadata(self, mock_fetch: MagicMock):
        """A capture (scrubber set) must not persist query/userinfo in metadata."""
        from quarry.ingestion.pipeline import ingest_url
        from quarry.scrub import scrub_and_log

        mock_fetch.return_value = (
            "<html><body><p>Reset your password.</p></body></html>"
        )
        settings = _fake_settings()
        db = _fake_db()

        recorded: dict[str, str] = {}
        real_extract = HtmlExtractor.extract_from_html

        def spy(
            self: HtmlExtractor, html: str, document_name: str, document_path: str
        ) -> Any:
            recorded["document_path"] = document_path
            return real_extract(self, html, document_name, document_path)

        url = "https://x.test/reset?email=user@example.com&token=abc123secret"
        with (
            patch(
                "quarry.ingestion.streaming.get_embedding_backend",
            ) as mock_embed_factory,
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
            patch.object(HtmlExtractor, "extract_from_html", spy),
        ):
            mock_embed_factory.return_value = _fake_backend()
            result = ingest_url(
                url,
                db,
                settings,
                content_scrubber=lambda t: scrub_and_log(t, "test"),
            )

        name = result["document_name"]
        path = recorded["document_path"]
        assert name == "https://x.test/reset"
        assert path == "https://x.test/reset"
        for leaked in ("user@example.com", "token=abc123secret", "abc123secret"):
            assert leaked not in name
            assert leaked not in path
        # The fetch itself still uses the full URL — the secret path is needed to
        # retrieve the page; only the persisted metadata is redacted.
        mock_fetch.assert_called_once_with(url)

    @patch(_FETCH)
    def test_plain_ingest_keeps_full_url(self, mock_fetch: MagicMock):
        """A user-initiated ingest (no scrubber) keeps the full URL as metadata."""
        from quarry.ingestion.pipeline import ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        url = "https://x.test/reset?email=user@example.com&token=abc123secret"
        with (
            patch(
                "quarry.ingestion.streaming.get_embedding_backend",
            ) as mock_embed_factory,
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            mock_embed_factory.return_value = _fake_backend()
            result = ingest_url(url, db, settings)

        assert result["document_name"] == url


class TestIngestRouteKeying:
    """The route keys the queue on the resolved table, not the body collection.

    quarry-ickn: a plain ingest with an omitted collection derives the URL host
    INSIDE the job, so keying the queue on the empty body collection would route
    it to a different worker than an explicit ``collection=<host>`` request for
    the same host — two writers on one table. The route now resolves the table
    before building the job, so the key always equals the table it writes.
    """

    def test_explicit_host_and_empty_key_the_same_collection(self) -> None:
        """collection=host and an omitted collection resolve to one queue key."""
        routes = IngestionRoutes(cast("DaemonContext", SimpleNamespace()))
        explicit = asyncio.run(
            routes._ingest_job(
                {"source": "https://example.com/a", "collection": "example.com"},
                "https://example.com/a",
            )
        )
        empty = asyncio.run(
            routes._ingest_job(
                {"source": "https://example.com/b"}, "https://example.com/b"
            )
        )
        assert isinstance(explicit, IngestJob)
        assert isinstance(empty, IngestJob)
        assert explicit.collection == empty.collection == "example.com"

    def test_distinct_hosts_key_distinct_collections(self) -> None:
        """Ingests to different hosts still route to their own workers."""
        routes = IngestionRoutes(cast("DaemonContext", SimpleNamespace()))
        a = asyncio.run(
            routes._ingest_job({"source": "https://a.test/x"}, "https://a.test/x")
        )
        b = asyncio.run(
            routes._ingest_job({"source": "https://b.test/y"}, "https://b.test/y")
        )
        assert isinstance(a, IngestJob)
        assert isinstance(b, IngestJob)
        assert a.collection == "a.test"
        assert b.collection == "b.test"


def _fake_settings() -> MagicMock:
    """Settings stub with the chunking/embedding knobs ingest_url reads."""
    settings = MagicMock()
    settings.chunk_max_chars = 1800
    settings.chunk_overlap_chars = 200
    settings.sync_flush_mb = 32
    settings.embed_window_chunks = 512
    return settings


def _fake_db() -> Database:
    """Database facade over a mocked LanceDB connection."""
    mock_lance = MagicMock()
    mock_lance.open_table.return_value = MagicMock()
    return Database(mock_lance)


def _fake_backend() -> MagicMock:
    """Embedding backend stub that returns zero vectors of the right shape."""
    backend = MagicMock()
    backend.model_name = "test-model"
    backend.embed_texts.side_effect = lambda texts: np.zeros(
        (len(texts), 768), dtype=np.float32
    )
    return backend
