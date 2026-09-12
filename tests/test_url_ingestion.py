"""Tests for URL ingestion: process HTML, chunk, embed, store."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

from quarry.daemon.ingest_jobs import IngestJob, ScrubbedIngestJob
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
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        mock_fetch.return_value = (
            "<html><head><title>Docs</title></head>"
            "<body><h1>API Reference</h1>"
            "<p>The authenticate endpoint accepts a JWT token.</p>"
            "</body></html>"
        )
        settings = _fake_settings()
        db = _fake_db()

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            result = ingest_url(
                UrlIngest("https://docs.example.com/api"),
                Progress(None),
                IngestContext(db, settings, collection="docs"),
            )

        assert result["document_name"] == "https://docs.example.com/api"
        assert result["collection"] == "docs"
        assert result["chunks"] >= 1
        mock_fetch.assert_called_once_with("https://docs.example.com/api")

    @patch(_FETCH)
    def test_custom_document_name(self, mock_fetch: MagicMock):
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            result = ingest_url(
                UrlIngest("https://example.com/page", document_name="my-page"),
                Progress(None),
                IngestContext(db, settings),
            )

        assert result["document_name"] == "my-page"

    @patch(_FETCH)
    def test_capture_path_redacts_url_metadata(self, mock_fetch: MagicMock):
        """A capture (scrubber set) must not persist query/userinfo in metadata."""
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url
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
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
            patch.object(HtmlExtractor, "extract_from_html", spy),
        ):
            result = ingest_url(
                UrlIngest(url, content_scrubber=lambda t: scrub_and_log(t, "test")),
                Progress(None),
                IngestContext(db, settings),
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
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        url = "https://x.test/reset?email=user@example.com&token=abc123secret"
        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            result = ingest_url(
                UrlIngest(url), Progress(None), IngestContext(db, settings)
            )

        assert result["document_name"] == url

    @patch(_FETCH)
    def test_plain_ingest_progress_redacts_fetch_url(self, mock_fetch: MagicMock):
        """The "Fetching: %s" progress line must redact the URL even for a
        plain (unscrubbed) ingest -- meta_url is deliberately the raw URL for
        document_name/document_path persistence (test_plain_ingest_keeps_full_url,
        above), but a sitemap/bulk-crawl worker builds that same unscrubbed
        UrlIngest for a program-discovered URL it never reviewed, and logging
        it verbatim would leak a userinfo/query secret into quarry.log
        (CWE-532) regardless of scrub status.

        Checks only the "Fetching:" message: ChunkStoreFunnel._embed_and_store's
        own "Done: %d chunks indexed from %s" progress line further downstream
        still echoes document_name unredacted for a plain ingest -- that shared,
        non-URL-aware choke point (used identically by ingest_content, which has
        no URL at all) has no way to know a given document_name is a URL, so it
        cannot redact one safely; reshaping it is separate work.
        """
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        url = "https://user:pass@x.test/reset?email=user@example.com&token=abc123secret"
        messages: list[str] = []
        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            result = ingest_url(
                UrlIngest(url), Progress(messages.append), IngestContext(db, settings)
            )

        # Metadata persistence is untouched: the plain-ingest policy still
        # keeps the full URL as the document's identity.
        assert result["document_name"] == url
        fetching = [m for m in messages if m.startswith("Fetching:")]
        assert fetching
        assert "user:pass" not in fetching[0]
        assert "token=abc123secret" not in fetching[0]
        # Exact match, not a host/path substring check (CodeQL
        # py/incomplete-url-substring-sanitization): pins the whole
        # redacted message rather than a fragment that could also match an
        # unrelated look-alike string.
        assert fetching == ["Fetching: https://x.test/reset"]

    @patch(_FETCH)
    def test_prefetched_html_skips_the_network_fetch(self, mock_fetch: MagicMock):
        """A caller that already fetched the body must not pay for a second fetch.

        The daemon's capture re-fetch (``CaptureIngestJob._refetch``) calls
        ``WebFetcher.fetch_body`` itself to decide HTML vs. text routing;
        ``prefetched_html`` lets ``ingest_url`` reuse that body instead of
        fetching the URL again (Copilot round-4, PR #496).
        """
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        settings = _fake_settings()
        db = _fake_db()
        html = (
            "<html><body><h1>Already fetched</h1><p>Some body text.</p></body></html>"
        )

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
        ):
            result = ingest_url(
                UrlIngest("https://example.com/page", prefetched_html=html),
                Progress(None),
                IngestContext(db, settings),
            )

        mock_fetch.assert_not_called()
        assert result["chunks"] >= 1

    @patch(_FETCH)
    def test_delay_sleeps_before_fetching(self, mock_fetch: MagicMock):
        """A positive delay sleeps (delay + sub-second jitter) before fetching."""
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
            patch("quarry.ingestion.web_ingest.time.sleep") as mock_sleep,
        ):
            ingest_url(
                UrlIngest("https://example.com/page", delay=0.5),
                Progress(None),
                IngestContext(db, settings),
            )

        mock_sleep.assert_called_once()
        (slept,) = mock_sleep.call_args.args
        assert 0.5 <= slept < 1.5  # delay plus up to 1s of sub-second jitter
        mock_fetch.assert_called_once()

    @patch(_FETCH)
    def test_negative_delay_does_not_sleep_or_crash(self, mock_fetch: MagicMock):
        """A negative delay must not reach time.sleep(delay + jitter) and raise."""
        from quarry.ingestion.ingest_context import IngestContext, Progress
        from quarry.ingestion.web_ingest import UrlIngest, ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"
        settings = _fake_settings()
        db = _fake_db()

        with (
            patch("quarry.db.chunk_store.ChunkStore.insert_records", return_value=1),
            patch("quarry.ingestion.web_ingest.time.sleep") as mock_sleep,
        ):
            ingest_url(
                UrlIngest("https://example.com/page", delay=-1.0),
                Progress(None),
                IngestContext(db, settings),
            )

        mock_sleep.assert_not_called()
        mock_fetch.assert_called_once()


class TestRememberCollectionRouting:
    """Server-side sentinel: empty ``collection`` routes by ``agent_handle``.

    One rule at the daemon chokepoint keeps the CLI/MCP/HTTP surfaces from
    drifting (bug class 3): explicit collection wins; empty + handle lands in
    ``memory-<handle>``; empty on both sides falls back to ``default``.
    """

    @staticmethod
    def _job(body: dict[str, object]) -> ScrubbedIngestJob:
        routes = IngestionRoutes(cast("DaemonContext", SimpleNamespace()))
        payload = {"name": "note.md", "content": "hi", **body}
        job = routes._remember_job(payload)
        assert isinstance(job, ScrubbedIngestJob), job
        return job

    def test_handle_and_empty_collection_route_to_memory_bucket(self) -> None:
        assert self._job({"agent_handle": "rmh"}).collection == "memory-rmh"

    def test_explicit_collection_wins_over_agent_handle(self) -> None:
        job = self._job({"agent_handle": "rmh", "collection": "notes"})
        assert job.collection == "notes"

    def test_empty_handle_and_empty_collection_fall_back_to_default(self) -> None:
        assert self._job({}).collection == "default"

    def test_empty_handle_with_explicit_collection_uses_that_collection(self) -> None:
        job = self._job({"collection": "research"})
        assert job.collection == "research"


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
