"""Tests for the quarry HTTP server (quarryd).

Uses Starlette's TestClient with mocked database and embedding backends.
Each test class gets its own app instance via fixtures.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import stat
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.api import HealthResponse, SearchResponse, StatusResponse
from quarry.backfill import BackfillStats
from quarry.captures_collection import CapturesCollection
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.daemon.server import DaemonServer, ServeConfig
from quarry.daemon.tasks import TASK_TTL_SECONDS, TaskState
from quarry.results import SearchResult


def _poll_task_done(
    tc: TestClient, task_id: str, max_polls: int = 100
) -> dict[str, Any]:
    """Poll GET /tasks/{task_id} until a terminal status, returning the final JSON.

    Sleeps between polls so the background thread — which does a real LanceDB
    connect on its first ``ctx.database`` access — can finish.  A task still
    ``queued``/``running`` when the budget drains is a bug, not a pass: returning
    that non-terminal snapshot lets a caller's assertions silently not run (the
    mock never fired), so raise instead.  The queue interposes a ``queued`` state
    before ``running`` (DES-042), so both are non-terminal here.
    """
    for _ in range(max_polls):
        data: dict[str, Any] = tc.get(f"/v1/tasks/{task_id}").json()
        if data["status"] not in {"queued", "running"}:
            return data
        time.sleep(0.05)
    msg = f"task {task_id} still pending after {max_polls} polls"
    raise AssertionError(msg)


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.registry_path = tmp_path / "registry.db"  # does not exist -> regs = []
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    s.ingest_queue_depth = 32
    s.ingest_embed_concurrency = 1
    s.ingest_drain_timeout_s = 30.0
    s.ingest_max_workers = 256
    s.ingest_worker_idle_s = 60.0
    # These HTTP tests do not exercise the watch loop; keep it inert so a
    # lifespan-running test never starts a real observer or safety timer.
    s.watch_enabled = False
    return s


def _mock_embedder() -> MagicMock:
    embedder = MagicMock()
    embedder.embed_query.return_value = np.zeros(768, dtype=np.float32)
    embedder.dimension = 768
    embedder.model_name = "test-model"
    return embedder


_SHARED_EMBEDDER = _mock_embedder()


def _inject_mocks(ctx: DaemonContext) -> None:
    """Replace the daemon's ONNX embedding session with a mock.

    ``embedder`` is a ``cached_property`` slot on ``ctx._resources``; writing
    into that instance ``__dict__`` short-circuits construction so tests never
    load the real ONNX model.  The DB connections (``database`` /
    ``query_database``) are left to build against the real per-test tmp_path
    LanceDB so route logic exercises a real (empty) database.
    """
    ctx._resources.__dict__["embedder"] = _SHARED_EMBEDDER


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Build a test app and yield a TestClient, draining background tasks on exit.

    Entering the ``TestClient`` context runs the app on a portal event loop; on
    teardown we cancel any still-running background ingest/sync task ON that loop
    before the next test installs its own mocks.  Otherwise a leaked ``usp``
    sitemap fetch resolves a hostname inside a later SSRF test's ``getaddrinfo``
    patch window, tripping its ``assert_not_called`` (an isolation-order flake).
    """
    settings = _mock_settings(tmp_path)
    ctx = DaemonContext(settings)
    _inject_mocks(ctx)

    app = build_app(ctx)
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc
        portal = tc.portal  # set for the lifetime of the context
        if portal is not None:
            portal.call(ctx.tasks.cancel_all)
            # Ingest jobs now run inside per-collection queue workers, not tracked
            # tasks, so stop those workers too or a queued job could run into a
            # later test's mock window (the same isolation guard as cancel_all).
            portal.call(ctx.ingest_queue.cancel_workers)


class TestHealth:
    def test_returns_ok(self, client: TestClient) -> None:
        data = client.get("/health").json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0

    def test_reports_full_v2_fields(self, client: TestClient) -> None:
        """/health carries state, api_version, and quarry_version (DES-031 v2.2)."""
        data = client.get("/health").json()
        # A fixture context is never warmed, so it reports the pre-ready state.
        assert data["state"] == "starting"
        assert data["api_version"] == "1"
        assert data["quarry_version"]

    def test_state_ready_after_warm(self, tmp_path: Path) -> None:
        """A warmed context flips /health state to ``ready``."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        ctx._resources.__dict__["database"] = MagicMock()
        ctx._resources.__dict__["query_database"] = MagicMock()
        ctx.warm()
        warmed = TestClient(build_app(ctx), raise_server_exceptions=False)
        assert warmed.get("/health").json()["state"] == "ready"

    def test_cors_headers(self, client: TestClient) -> None:
        resp = client.get("/health", headers={"Origin": "http://localhost"})
        assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"


class TestCaCertRoute:
    """Tests for the /ca.crt auth-exempt route."""

    def test_returns_404_when_no_cert(self, client: TestClient, tmp_path: Path) -> None:
        empty_tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", empty_tls_dir):
            resp = client.get("/ca.crt")
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data
        assert "quarry install" in data["error"]

    def test_returns_cert_pem(self, client: TestClient, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        fake_pem = "-----BEGIN CERTIFICATE-----\nfakecert\n-----END CERTIFICATE-----\n"
        (tls_dir / "ca.crt").write_text(fake_pem)
        with patch("quarry.tls.TLS_DIR", tls_dir):
            resp = client.get("/ca.crt")
        assert resp.status_code == 200
        assert "BEGIN CERTIFICATE" in resp.text

    def test_auth_exempt_without_api_key_check(self, tmp_path: Path) -> None:
        """The /ca.crt route bypasses auth even when an API key is set."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings, api_key="secret-key")
        _inject_mocks(ctx)
        auth_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        fake_pem = "-----BEGIN CERTIFICATE-----\nfakecert\n-----END CERTIFICATE-----\n"
        (tls_dir / "ca.crt").write_text(fake_pem)

        # No Authorization header -- should still get the cert.
        with patch("quarry.tls.TLS_DIR", tls_dir):
            resp = auth_client.get("/ca.crt")
        assert resp.status_code == 200


class TestConcurrency:
    """Verify the server handles concurrent requests without serializing."""

    async def test_concurrent_requests_overlap(self, tmp_path: Path) -> None:
        """Two slow /search requests must overlap, not serialize (DES-032).

        Each request blocks in a patched ``hybrid_search`` for ``delay``
        seconds.  ``_search_route`` is a sync route, so Starlette dispatches
        it to the threadpool and the event loop stays free to start the
        second request while the first sleeps.  Overlapping requests finish
        in roughly ``1x delay``; serialized requests take ``2x``.  The
        ``1.6x`` bound fails on serialization yet tolerates threadpool
        scheduling jitter.

        Driven via ``httpx.ASGITransport`` rather than Starlette's
        ``TestClient``: ``TestClient`` funnels every call through a single
        anyio portal thread, which serializes concurrent requests at the
        client and makes this test measure the client, not the server.
        """
        import time

        import httpx

        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)

        # Warm the cached connections single-threaded, exactly as serve()
        # does before accepting traffic.  Otherwise the first request pays
        # the one-time Database.connect cost inside the timed window and the
        # test measures cold-start, not steady-state route concurrency.
        ctx.warm()

        delay = 0.5

        def slow_search(*_args: object, **_kwargs: object) -> list[object]:
            time.sleep(delay)
            return []

        transport = httpx.ASGITransport(app=app)
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            side_effect=slow_search,
        ):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                start = time.monotonic()
                first, second = await asyncio.gather(
                    ac.get("/v1/search?q=a"),
                    ac.get("/v1/search?q=b"),
                )
                elapsed = time.monotonic() - start

        assert first.status_code == 200
        assert second.status_code == 200
        assert elapsed < 1.6 * delay, (
            f"Requests appear serialized: {elapsed:.2f}s >= {1.6 * delay:.2f}s"
        )


class TestSearch:
    def test_missing_query_returns_400(self, client: TestClient) -> None:
        assert client.get("/v1/search").status_code == 400

    def test_empty_query_returns_400(self, client: TestClient) -> None:
        assert client.get("/v1/search?q=").status_code == 400

    def test_search_returns_results(self, client: TestClient) -> None:
        mock_results = [
            {
                "document_name": "test.pdf",
                "collection": "default",
                "page_number": 1,
                "chunk_index": 0,
                "text": "Hello world",
                "page_type": "text",
                "source_format": ".pdf",
                "_distance": 0.1,
            }
        ]
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            return_value=[SearchResult.from_row(r) for r in mock_results],
        ):
            data = client.get("/v1/search?q=hello").json()

        assert data["query"] == "hello"
        assert data["total_results"] == 1
        assert data["results"][0]["document_name"] == "test.pdf"
        assert data["results"][0]["similarity"] == 0.9
        assert "summary" in data["results"][0]

    def test_search_result_includes_summary_field(self, client: TestClient) -> None:
        """HTTP /search results must include the summary field."""
        mock_results = [
            {
                "document_name": "doc.md",
                "collection": "default",
                "page_number": 1,
                "chunk_index": 2,
                "text": "some content",
                "page_type": "text",
                "source_format": ".md",
                "_distance": 0.2,
                "summary": "a brief summary",
            }
        ]
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            return_value=[SearchResult.from_row(r) for r in mock_results],
        ):
            data = client.get("/v1/search?q=content").json()

        result = data["results"][0]
        assert result["summary"] == "a brief summary"

    def test_search_result_summary_defaults_to_empty_string(
        self, client: TestClient
    ) -> None:
        """summary must default to empty string when absent from DB row."""
        mock_results = [
            {
                "document_name": "doc.md",
                "collection": "default",
                "page_number": 1,
                "chunk_index": 0,
                "text": "content",
                "page_type": "text",
                "source_format": ".md",
                "_distance": 0.1,
                # no summary key
            }
        ]
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            return_value=[SearchResult.from_row(r) for r in mock_results],
        ):
            data = client.get("/v1/search?q=content").json()

        assert data["results"][0]["summary"] == ""

    def test_search_missing_distance_sinks_to_bottom(self, client: TestClient) -> None:
        """A row lacking _distance must serialize as similarity -1, not a fake 1.0."""
        mock_results = [
            {
                "document_name": "doc.md",
                "collection": "default",
                "page_number": 1,
                "chunk_index": 0,
                "text": "content",
                "page_type": "text",
                "source_format": ".md",
                # no _distance key (quarry-gcnf)
            }
        ]
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            return_value=[SearchResult.from_row(r) for r in mock_results],
        ):
            data = client.get("/v1/search?q=content").json()

        assert data["results"][0]["similarity"] == -1.0

    def test_search_with_limit(self, client: TestClient) -> None:
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&limit=5")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 5

    def test_search_limit_capped_at_50(self, client: TestClient) -> None:
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&limit=999")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 50

    def test_search_negative_limit_clamped_to_1(self, client: TestClient) -> None:
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&limit=-5")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 1

    def test_search_with_collection_filter(self, client: TestClient) -> None:
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&collection=research")

        _, kwargs = mock_search.call_args
        assert kwargs["search_filter"].collection == "research"

    def test_search_empty_results(self, client: TestClient) -> None:
        with patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]):
            data = client.get("/v1/search?q=nonexistent").json()

        assert data["total_results"] == 0
        assert data["results"] == []

    def test_search_agent_handle_filter_passed_through(
        self, client: TestClient
    ) -> None:
        """agent_handle query param must reach hybrid_search as agent_handle_filter."""
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&agent_handle=someagent")

        _, kwargs = mock_search.call_args
        assert kwargs["search_filter"].agent_handle == "someagent"

    def test_search_memory_type_filter_passed_through(self, client: TestClient) -> None:
        """memory_type query param must reach hybrid_search as memory_type_filter."""
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&memory_type=episodic")

        _, kwargs = mock_search.call_args
        assert kwargs["search_filter"].memory_type == "episodic"

    def test_search_document_filter_passed_through(self, client: TestClient) -> None:
        """document query param must reach hybrid_search as document_filter."""
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as mock_search:
            client.get("/v1/search?q=hello&document=report.pdf")

        _, kwargs = mock_search.call_args
        assert kwargs["search_filter"].document == "report.pdf"

    def test_search_result_includes_agent_and_memory_fields(
        self, client: TestClient
    ) -> None:
        """Results must include agent_handle and memory_type fields."""
        mock_results = [
            {
                "document_name": "note.md",
                "collection": "default",
                "page_number": 1,
                "chunk_index": 0,
                "text": "remember this",
                "page_type": "text",
                "source_format": ".md",
                "agent_handle": "rmh",
                "memory_type": "episodic",
                "_distance": 0.2,
            }
        ]
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            return_value=[SearchResult.from_row(r) for r in mock_results],
        ):
            data = client.get("/v1/search?q=remember").json()

        result = data["results"][0]
        assert result["agent_handle"] == "rmh"
        assert result["memory_type"] == "episodic"


class TestDocuments:
    def test_list_documents(self, client: TestClient) -> None:
        mock_docs = [
            {
                "document_name": "report.pdf",
                "document_path": "/path/to/report.pdf",
                "collection": "default",
                "total_pages": 10,
                "chunk_count": 25,
                "indexed_pages": 10,
                "ingestion_timestamp": "2026-01-01T00:00:00",
            }
        ]
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=mock_docs,
        ):
            data = client.get("/v1/documents").json()

        assert data["total_documents"] == 1
        assert data["documents"][0]["document_name"] == "report.pdf"

    def test_list_documents_with_collection_filter(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ) as mock_list:
            client.get("/v1/documents?collection=research")

        _, kwargs = mock_list.call_args
        assert kwargs["collection_filter"] == "research"


class TestCollections:
    def test_list_collections(self, client: TestClient) -> None:
        mock_cols = [{"collection": "default", "document_count": 3, "chunk_count": 50}]
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_collections",
            return_value=mock_cols,
        ):
            data = client.get("/v1/collections").json()

        assert data["total_collections"] == 1
        assert data["collections"][0]["collection"] == "default"


class TestStatus:
    def test_returns_status(self, client: TestClient) -> None:
        with (
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=0),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections", return_value=[]
            ),
        ):
            data = client.get("/v1/status").json()

        assert data["document_count"] == 0
        assert data["chunk_count"] == 0
        assert data["collection_count"] == 0
        assert "database_path" in data
        assert "embedding_model" in data

    def test_registered_directories_present_and_integer(self, tmp_path: Path) -> None:
        """registered_directories must appear in the /status response as an int."""
        settings = _mock_settings(tmp_path)
        # Create the registry file so registry_path.exists() returns True.
        settings.registry_path.touch()
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        reg_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        fake_regs = [MagicMock(), MagicMock()]
        with (
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=0),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections", return_value=[]
            ),
            patch("quarry.daemon.routes.meta.SyncRegistry") as mock_registry,
        ):
            mock_registry.return_value.list_registrations.return_value = fake_regs
            data = reg_client.get("/v1/status").json()

        assert "registered_directories" in data
        assert isinstance(data["registered_directories"], int)
        assert data["registered_directories"] == 2

    def test_registered_directories_zero_when_no_registry(self, tmp_path: Path) -> None:
        """When registry_path does not exist, registered_directories must be 0."""
        settings = _mock_settings(tmp_path)
        # registry_path points to a non-existent file
        settings.registry_path = tmp_path / "no-registry.db"
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        no_reg_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        with (
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=0),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections", return_value=[]
            ),
        ):
            data = no_reg_client.get("/v1/status").json()

        assert data["registered_directories"] == 0


class TestServeToken:
    """DaemonServer persists the loopback bearer so clients can authenticate."""

    def _server(self, tmp_path: Path, api_key: str | None) -> DaemonServer:
        settings = MagicMock()
        settings.lancedb_path = tmp_path / "default" / "lancedb"
        config = ServeConfig(host="127.0.0.1", port=8420, api_key=api_key)
        return DaemonServer(settings, config)

    @staticmethod
    def _bound_server_mock() -> MagicMock:
        """A uvicorn.Server mock whose startup succeeds and reports a bound port."""
        server = MagicMock()

        async def _startup(sockets: object = None) -> None:
            return None

        server.startup = _startup
        sock = MagicMock()
        sock.getsockname.return_value = ("127.0.0.1", 8420)
        server.servers = [MagicMock(sockets=[sock])]
        return server

    def test_startup_hook_writes_token_after_bind(self, tmp_path: Path) -> None:
        server = self._server(tmp_path, "the-bearer")
        uv = self._bound_server_mock()
        server._install_startup_hook(uv)
        asyncio.run(uv.startup())
        token_path = tmp_path / "default" / "serve.token"
        assert token_path.read_text() == "the-bearer"
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert server._bound is True

    def test_ipv6_start_log_brackets_host(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The run()-start log must also render a valid host:port for an IPv6 bind
        # ([::1]:8420), not the ambiguous ::1:8420. Display only; bind unchanged.
        settings = MagicMock()
        settings.lancedb_path = tmp_path / "default" / "lancedb"
        server = DaemonServer(settings, ServeConfig(host="::1", port=8420, api_key="k"))
        with (
            patch("quarry.daemon.server.DaemonContext"),
            patch("quarry.daemon.server.build_app"),
            patch("quarry.daemon.server.uvicorn.Server"),
            caplog.at_level(logging.INFO, logger="quarry.daemon.server"),
        ):
            server.run()
        starting = [
            r.getMessage()
            for r in caplog.records
            if "Starting Quarry server" in r.getMessage()
        ]
        assert starting, "no start log emitted"
        assert "[::1]:8420" in starting[0]
        assert " on ::1:8420" not in starting[0]  # never the ambiguous bare form

    def test_ipv6_listening_log_brackets_host(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The startup log must render a valid URL for an IPv6 bind — [::1]:8420,
        # never the ambiguous bare ::1:8420 — via to_netloc. Display only; the
        # bind host is unchanged.
        settings = MagicMock()
        settings.lancedb_path = tmp_path / "default" / "lancedb"
        server = DaemonServer(settings, ServeConfig(host="::1", port=8420, api_key="k"))
        uv = self._bound_server_mock()
        server._install_startup_hook(uv)
        with caplog.at_level(logging.INFO, logger="quarry.daemon.server"):
            asyncio.run(uv.startup())
        listening = [
            r.getMessage() for r in caplog.records if "listening on" in r.getMessage()
        ]
        assert listening, "no listening log emitted"
        assert "[::1]:8420" in listening[0]
        assert "://::1:8420" not in listening[0]  # never the ambiguous bare form

    @pytest.mark.parametrize("api_key", [None, "", "   ", "\n"])
    def test_refuses_bind_without_key(
        self, tmp_path: Path, api_key: str | None
    ) -> None:
        """A loopback bind with no (or whitespace-only) key is refused (R4).

        Auth can never silently disable: the launcher mints a loopback token,
        and a caller that passes none is refused rather than run open.
        """
        with pytest.raises(SystemExit, match="without an API key"):
            self._server(tmp_path, api_key)

    def test_strips_operator_key_whitespace(self, tmp_path: Path) -> None:
        """An operator key with a trailing newline is stripped once at the
        daemon boundary, so serve.token matches what the stripping client reads.
        """
        server = self._server(tmp_path, "the-bearer\n")
        uv = self._bound_server_mock()
        server._install_startup_hook(uv)
        asyncio.run(uv.startup())
        # serve.token holds the stripped value — the loopback client reads and
        # strips serve.token, so the daemon's auth value must be stripped too.
        assert (tmp_path / "default" / "serve.token").read_text() == "the-bearer"

    def test_failed_bind_leaves_peer_token_intact(self, tmp_path: Path) -> None:
        """A second quarryd failing to bind must not clobber a running peer.

        The token is written only AFTER a successful bind, so instance #2 (which
        fails to bind) never writes it — and its cleanup, guarded by ``_bound``,
        never removes the shared serve.token that a running instance #1 owns.
        """
        (tmp_path / "default").mkdir(parents=True)
        peer_token = tmp_path / "default" / "serve.token"
        peer_token.write_text("peer-1-live-token")  # instance #1's live token

        server = self._server(tmp_path, "instance-2-token")
        with (
            patch("quarry.daemon.server.DaemonContext"),
            patch("quarry.daemon.server.build_app"),
            patch("quarry.daemon.server.uvicorn.Server") as mock_uv,
        ):
            mock_uv.return_value.run.side_effect = OSError("address already in use")
            with pytest.raises(OSError, match="address already in use"):
                server.run()

        assert peer_token.read_text() == "peer-1-live-token"  # untouched
        assert server._bound is False  # #2 never bound, so it wrote/removed nothing

    def test_clean_shutdown_removes_bound_instance_sidecars(
        self, tmp_path: Path
    ) -> None:
        """A bound instance removes its own serve.token + port file on clean exit.

        Covers the ``if self._bound:`` TRUE branch of the lifespan finally — the
        normal graceful-shutdown cleanup, distinct from the clobber-safety guard.
        """
        (tmp_path / "default").mkdir(parents=True)
        token = tmp_path / "default" / "serve.token"
        port = tmp_path / "default" / "serve.port"
        token.write_text("live-token")
        port.write_text("8420")

        server = self._server(tmp_path, "live-token")
        server._bound = True  # this instance bound successfully
        # The lifespan drains the ingest queue via app.state.ctx on shutdown, so
        # give it a real context whose (empty) queue closes cleanly.
        app = MagicMock()
        app.state.ctx = DaemonContext(_mock_settings(tmp_path / "ctx"))

        async def _drive() -> None:
            async with server._lifespan(app):
                assert token.exists()  # present while serving
                assert port.exists()

        asyncio.run(_drive())

        assert not token.exists()  # removed on clean shutdown
        assert not port.exists()

    def test_sidecar_write_failure_leaves_no_partial_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed sidecar write removes BOTH — no lone port file, no orphan token.

        The pair is written all-or-nothing (token first, then port); if the
        second write fails, the first is removed so startup never leaves a
        partial sidecar a client would read as "daemon up".
        """
        server = self._server(tmp_path, "the-bearer")
        uv = self._bound_server_mock()
        server._install_startup_hook(uv)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        # Token write succeeds; the port write fails after it.
        monkeypatch.setattr("quarry.run_dir.PortFile.write", _boom)
        with pytest.raises(OSError, match="disk full"):
            asyncio.run(uv.startup())

        assert not (tmp_path / "default" / "serve.token").exists()
        assert not (tmp_path / "default" / "serve.port").exists()
        assert server._bound is False  # not set until both writes succeed

    def test_token_write_failure_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the token write itself fails, no sidecar is removed (wrote_token=False).

        Peer-safety guard: the all-or-nothing cleanup removes ONLY what this
        instance wrote.  A keyed instance whose FIRST write (token) fails wrote
        nothing, so a peer's serve.port on the shared per-db path stays intact.
        """
        (tmp_path / "default").mkdir(parents=True)
        peer_port = tmp_path / "default" / "serve.port"
        peer_port.write_text("9999")  # a peer's port file on the shared path

        server = self._server(tmp_path, "the-bearer")
        uv = self._bound_server_mock()
        server._install_startup_hook(uv)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("quarry.run_dir.ServeTokenFile.write", _boom)
        with pytest.raises(OSError, match="disk full"):
            asyncio.run(uv.startup())

        assert peer_port.read_text() == "9999"  # untouched — token write failed first
        assert server._bound is False

    def test_base_exception_between_writes_leaves_no_lone_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SIGINT/KeyboardInterrupt landing AFTER serve.token but BEFORE
        serve.port must not leave a lone serve.token — a false "daemon up"
        signal to loopback clients.  The cleanup catches BaseException, removes
        only what this instance wrote, and re-raises."""
        server = self._server(tmp_path, "the-bearer")
        uv = self._bound_server_mock()
        server._install_startup_hook(uv)

        def _interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        # Token write succeeds; the port write is interrupted (not an OSError).
        monkeypatch.setattr("quarry.run_dir.PortFile.write", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(uv.startup())

        assert not (tmp_path / "default" / "serve.token").exists()  # no lone token
        assert not (tmp_path / "default" / "serve.port").exists()
        assert server._bound is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the run-dir lock is POSIX-only (fcntl is unavailable on Windows)",
)
class TestRunDirLock:
    """Exactly one daemon may own a run dir — an exclusive advisory flock closes
    the shared-run-dir clobber (a second daemon on a different port overwriting
    the first's serve.token) and the token-writer's temp-retry race.

    POSIX-only: the whole class is skipped where ``fcntl`` is unavailable."""

    def _server(self, tmp_path: Path, api_key: str) -> DaemonServer:
        settings = MagicMock()
        settings.lancedb_path = tmp_path / "default" / "lancedb"  # parent runs dir
        config = ServeConfig(host="127.0.0.1", port=8420, api_key=api_key)
        return DaemonServer(settings, config)

    def test_second_daemon_refused_and_first_token_untouched(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "default").mkdir(parents=True)
        peer_token = tmp_path / "default" / "serve.token"
        peer_token.write_text("first-daemon-token")  # daemon #1's live token

        first = self._server(tmp_path, "first")
        first._acquire_run_dir_lock()
        try:
            second = self._server(tmp_path, "second")
            with pytest.raises(SystemExit, match="already owns this database"):
                second._acquire_run_dir_lock()
            # #2 was refused BEFORE writing anything — #1's token is intact.
            assert peer_token.read_text() == "first-daemon-token"
            assert second._lock_fd < 0  # #2 never took the lock
        finally:
            first._release_run_dir_lock()

    def test_lock_released_allows_reacquire(self, tmp_path: Path) -> None:
        # The lock releases on exit, so a restart (or a distinct daemon after
        # the first exits) can re-acquire the same run dir.
        first = self._server(tmp_path, "first")
        first._acquire_run_dir_lock()
        first._release_run_dir_lock()

        second = self._server(tmp_path, "second")
        second._acquire_run_dir_lock()  # succeeds now that #1 released
        assert second._lock_fd >= 0
        second._release_run_dir_lock()

    def test_flock_failure_closes_fd_and_propagates(self, tmp_path: Path) -> None:
        # File-I/O hygiene: a NON-BlockingIOError flock failure (permission /
        # filesystem) must close the fd — never leak a descriptor (EMFILE
        # history) — and propagate.  _lock_fd stays unset so release is a no-op.
        server = self._server(tmp_path, "k")
        closed: list[int] = []
        real_close = os.close

        def _spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with (
            patch(
                "quarry.daemon.server.fcntl.flock",
                side_effect=PermissionError("denied"),
            ),
            patch("quarry.daemon.server.os.close", side_effect=_spy_close),
            pytest.raises(PermissionError, match="denied"),
        ):
            server._acquire_run_dir_lock()

        assert len(closed) == 1  # the lock fd was closed exactly once (no leak)
        assert server._lock_fd < 0  # never recorded a live fd on failure

    def test_lock_fd_is_cloexec(self, tmp_path: Path) -> None:
        # The serve.lock fd must be O_CLOEXEC so it is not leaked into (and does
        # not retain the lock across) subprocesses the daemon spawns.
        import fcntl  # POSIX-only; the class is skipped where it is absent

        server = self._server(tmp_path, "k")
        server._acquire_run_dir_lock()
        try:
            flags = fcntl.fcntl(server._lock_fd, fcntl.F_GETFD)
            assert flags & fcntl.FD_CLOEXEC
        finally:
            server._release_run_dir_lock()

    def test_release_is_best_effort_and_never_raises(self, tmp_path: Path) -> None:
        # release() runs in run()'s finally: a failing LOCK_UN must be swallowed
        # (logged), never re-raised — else it would MASK the real shutdown
        # reason. The fd is still closed (fd close releases the lock anyway).
        server = self._server(tmp_path, "k")
        server._acquire_run_dir_lock()
        held_fd = server._lock_fd
        closed: list[int] = []
        real_close = os.close

        def _spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with (
            patch("quarry.daemon.server.fcntl.flock", side_effect=OSError("EINTR")),
            patch("quarry.daemon.server.os.close", side_effect=_spy_close),
        ):
            server._release_run_dir_lock()  # must NOT raise

        assert held_fd in closed  # fd closed despite the LOCK_UN failure
        assert server._lock_fd < 0  # cleared

    def test_release_does_not_mask_an_in_flight_exception(self, tmp_path: Path) -> None:
        # Simulate run()'s finally: an exception is already in flight when
        # release() is called and LOCK_UN also fails. The original exception
        # must propagate, not the harmless unlock error.
        server = self._server(tmp_path, "k")
        server._acquire_run_dir_lock()
        with (
            patch("quarry.daemon.server.fcntl.flock", side_effect=OSError("EINTR")),
            pytest.raises(RuntimeError, match="original shutdown failure"),
        ):
            try:
                raise RuntimeError("original shutdown failure")
            finally:
                server._release_run_dir_lock()

    def test_release_swallows_a_failing_os_close(self, tmp_path: Path) -> None:
        # os.close can ALSO raise (EBADF/EINTR); release must swallow+log it so
        # the method truly never raises. _lock_fd is still cleared.
        server = self._server(tmp_path, "k")
        server._acquire_run_dir_lock()
        with patch("quarry.daemon.server.os.close", side_effect=OSError("EBADF")):
            server._release_run_dir_lock()  # must NOT raise
        assert server._lock_fd < 0

    def test_failing_os_close_does_not_mask_in_flight_exception(
        self, tmp_path: Path
    ) -> None:
        # Even when os.close raises in release (called from run()'s finally), the
        # original shutdown exception must propagate, not the close error.
        server = self._server(tmp_path, "k")
        server._acquire_run_dir_lock()
        with (
            patch("quarry.daemon.server.os.close", side_effect=OSError("EBADF")),
            pytest.raises(RuntimeError, match="original shutdown failure"),
        ):
            try:
                raise RuntimeError("original shutdown failure")
            finally:
                server._release_run_dir_lock()

    def test_acquire_fails_closed_when_fcntl_unavailable(self, tmp_path: Path) -> None:
        # Portability: fcntl is POSIX-only, imported optionally (None on a
        # non-POSIX platform).  A daemon start there must fail closed with a
        # clear message — never a silent no-op that reopens the clobber race,
        # never a raw AttributeError from dereferencing None.
        server = self._server(tmp_path, "k")
        with (
            patch("quarry.daemon.server.fcntl", None),
            pytest.raises(SystemExit, match="POSIX"),
        ):
            server._acquire_run_dir_lock()
        assert server._lock_fd < 0  # no fd opened when the lock is unavailable


class TestNotFound:
    def test_unknown_path_returns_404(self, client: TestClient) -> None:
        resp = client.get("/unknown")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Not Found"


class TestOptionsPreflightCors:
    def test_options_returns_200(self, client: TestClient) -> None:
        # Starlette CORSMiddleware returns 200 for preflight OPTIONS.
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200

    def test_cors_allows_authorization_header(self, client: TestClient) -> None:
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        allow_headers = resp.headers.get("Access-Control-Allow-Headers", "")
        tokens = [h.strip().lower() for h in allow_headers.split(",")]
        assert "authorization" in tokens

    def test_cors_allows_post_method(self, client: TestClient) -> None:
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "POST",
            },
        )
        allow_methods = resp.headers.get("Access-Control-Allow-Methods", "")
        tokens = [m.strip().upper() for m in allow_methods.split(",")]
        assert "POST" in tokens

    def test_cors_allows_delete_method(self, client: TestClient) -> None:
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        allow_methods = resp.headers.get("Access-Control-Allow-Methods", "")
        tokens = [m.strip().upper() for m in allow_methods.split(",")]
        assert "DELETE" in tokens


class TestCorsOrigins:
    """Test configurable CORS origin reflection."""

    @pytest.fixture()
    def cors_client(self, tmp_path: Path) -> TestClient:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(
            settings,
            cors_origins=frozenset(
                {
                    "https://punt-labs.com",
                    "http://localhost:4321",
                }
            ),
        )
        _inject_mocks(ctx)

        app = build_app(ctx)
        return TestClient(app, raise_server_exceptions=False)

    def test_matching_origin_reflected(self, cors_client: TestClient) -> None:
        resp = cors_client.get("/health", headers={"Origin": "https://punt-labs.com"})
        assert resp.headers["Access-Control-Allow-Origin"] == "https://punt-labs.com"

    def test_second_origin_reflected(self, cors_client: TestClient) -> None:
        resp = cors_client.get("/health", headers={"Origin": "http://localhost:4321"})
        assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:4321"

    def test_non_matching_origin_no_cors_headers(self, cors_client: TestClient) -> None:
        resp = cors_client.get("/health", headers={"Origin": "https://evil.com"})
        assert resp.headers.get("Access-Control-Allow-Origin") is None

    def test_no_origin_header_no_cors_headers(self, cors_client: TestClient) -> None:
        resp = cors_client.get("/health")
        assert resp.headers.get("Access-Control-Allow-Origin") is None

    def test_options_reflects_matching_origin(self, cors_client: TestClient) -> None:
        resp = cors_client.options(
            "/health",
            headers={
                "Origin": "https://punt-labs.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["Access-Control-Allow-Origin"] == "https://punt-labs.com"

    def test_default_client_allows_localhost(self, client: TestClient) -> None:
        """Default fixture has no cors_origins -- falls back to http://localhost."""
        resp = client.get("/health", headers={"Origin": "http://localhost"})
        assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"


# --- API key auth tests ---

_TEST_API_KEY = "test-key-for-auth-testing"


@pytest.fixture()
def auth_client(tmp_path: Path) -> TestClient:
    """Build a test app with API key auth enabled."""
    settings = _mock_settings(tmp_path)
    ctx = DaemonContext(settings, api_key=_TEST_API_KEY)
    _inject_mocks(ctx)

    app = build_app(ctx)
    return TestClient(app, raise_server_exceptions=False)


class TestApiKeyAuth:
    """Test Bearer token authentication on the HTTP server."""

    def test_health_exempt_without_key(self, auth_client: TestClient) -> None:
        data = auth_client.get("/health").json()
        assert data["status"] == "ok"

    def test_search_rejected_without_key(self, auth_client: TestClient) -> None:
        assert auth_client.get("/v1/search?q=test").status_code == 401

    def test_search_rejected_with_wrong_key(self, auth_client: TestClient) -> None:
        resp = auth_client.get(
            "/v1/search?q=test", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401

    def test_search_allowed_with_correct_key(self, auth_client: TestClient) -> None:
        with patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]):
            data = auth_client.get(
                "/v1/search?q=test",
                headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
            ).json()
        assert data["query"] == "test"

    def test_documents_rejected_without_key(self, auth_client: TestClient) -> None:
        assert auth_client.get("/v1/documents").status_code == 401

    def test_documents_allowed_with_key(self, auth_client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ):
            data = auth_client.get(
                "/v1/documents",
                headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
            ).json()
        assert data["total_documents"] == 0

    def test_status_rejected_without_key(self, auth_client: TestClient) -> None:
        """/v1/status is protected — it shares MetaRoutes with the exempt routes,
        so lock the trust boundary: only /health and /ca.crt skip auth."""
        assert auth_client.get("/v1/status").status_code == 401

    def test_no_auth_required_when_key_not_configured(self, client: TestClient) -> None:
        """The default client fixture has no api_key -- all open."""
        with patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]):
            data = client.get("/v1/search?q=test").json()
        assert data["query"] == "test"

    def test_malformed_auth_header_rejected(self, auth_client: TestClient) -> None:
        resp = auth_client.get(
            "/v1/search?q=test", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert resp.status_code == 401

    def test_bearer_scheme_case_insensitive(self, auth_client: TestClient) -> None:
        """RFC 7235: auth scheme names are case-insensitive."""
        with patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]):
            data = auth_client.get(
                "/v1/search?q=test",
                headers={"Authorization": f"bearer {_TEST_API_KEY}"},
            ).json()
        assert data["query"] == "test"


class TestEmptyApiKey:
    """Empty API key string should not enable auth."""

    @pytest.fixture()
    def empty_key_client(self, tmp_path: Path) -> TestClient:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings, api_key="")
        _inject_mocks(ctx)

        app = build_app(ctx)
        return TestClient(app, raise_server_exceptions=False)

    def test_empty_key_does_not_require_auth(
        self, empty_key_client: TestClient
    ) -> None:
        with patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]):
            data = empty_key_client.get("/v1/search?q=test").json()
        assert data["query"] == "test"


class TestShow:
    """Tests for GET /show endpoint."""

    def test_show_page_text(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.get_page_text",
            return_value="Hello world",
        ):
            data = client.get("/v1/show?document=foo&page=1").json()

        assert data["document_name"] == "foo"
        assert data["page_number"] == 1
        assert data["text"] == "Hello world"

    def test_show_metadata(self, client: TestClient) -> None:
        mock_doc = {
            "document_name": "foo",
            "collection": "default",
            "total_pages": 10,
            "chunk_count": 42,
            "indexed_pages": 10,
            "ingestion_timestamp": "2026-01-01T00:00:00",
        }
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[mock_doc],
        ):
            data = client.get("/v1/show?document=foo").json()

        assert data["document_name"] == "foo"
        assert data["total_pages"] == 10

    def test_show_missing_document_param(self, client: TestClient) -> None:
        resp = client.get("/v1/show")
        assert resp.status_code == 400
        assert "document" in resp.json()["error"].lower()

    def test_show_empty_document_param(self, client: TestClient) -> None:
        resp = client.get("/v1/show?document=")
        assert resp.status_code == 400

    def test_show_page_not_found(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.get_page_text", return_value=None
        ):
            resp = client.get("/v1/show?document=foo&page=1")

        assert resp.status_code == 404
        assert resp.json()["error"] == "Not found"

    def test_show_document_not_found(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ):
            resp = client.get("/v1/show?document=missing")

        assert resp.status_code == 404
        assert resp.json()["error"] == "Not found"

    def test_show_with_collection(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.get_page_text",
            return_value="page text",
        ) as mock_get_page:
            client.get("/v1/show?document=foo&page=1&collection=math")

        _, kwargs = mock_get_page.call_args
        assert kwargs["collection"] == "math"

    def test_show_metadata_with_collection(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ) as mock_list:
            client.get("/v1/show?document=foo&collection=math")

        _, kwargs = mock_list.call_args
        assert kwargs["collection_filter"] == "math"

    def test_show_invalid_page_returns_400(self, client: TestClient) -> None:
        resp = client.get("/v1/show?document=foo&page=abc")
        assert resp.status_code == 400
        assert "Invalid page number" in resp.json()["error"]

    def test_show_negative_page_returns_400(self, client: TestClient) -> None:
        resp = client.get("/v1/show?document=foo&page=-1")
        assert resp.status_code == 400
        assert "must be >= 1" in resp.json()["error"]

    def test_show_zero_page_returns_400(self, client: TestClient) -> None:
        resp = client.get("/v1/show?document=foo&page=0")
        assert resp.status_code == 400
        assert "must be >= 1" in resp.json()["error"]


class TestDeleteDocuments:
    """Tests for DELETE /documents endpoint -- now returns 202."""

    def test_delete_document_returns_202(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.db.chunk_store.ChunkStore.delete_document", return_value=15),
        ):
            resp = tc.delete("/v1/documents?name=foo")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("delete-")

    def test_delete_document_missing_name(self, client: TestClient) -> None:
        resp = client.delete("/v1/documents")
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_delete_document_with_collection(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            patch(
                "quarry.db.chunk_store.ChunkStore.delete_document", return_value=5
            ) as mock_del,
            TestClient(app, raise_server_exceptions=False) as tc,
        ):
            resp = tc.delete("/v1/documents?name=foo&collection=math")
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            _poll_task_done(tc, task_id)
            # Background task should have called db_delete_document with collection.
            mock_del.assert_called_once()
            _, kwargs = mock_del.call_args
            assert kwargs["collection"] == "math"


class TestDeleteCollections:
    """Tests for DELETE /collections endpoint -- now returns 202."""

    def test_delete_collection_returns_202(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.db.chunk_store.ChunkStore.delete_collection", return_value=50
            ),
        ):
            resp = tc.delete("/v1/collections?name=math")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("delete-")

    def test_delete_collection_missing_name(self, client: TestClient) -> None:
        resp = client.delete("/v1/collections")
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()


class TestCapture:
    """Tests for POST /v1/capture -- server-derived collection, always scrubs."""

    def test_success_returns_202(self, tmp_path: Path) -> None:
        mock_result = {
            "document_name": "session-abcd1234",
            "collection": "default-captures",
            "chunks": 2,
        }
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.ingestion.pipeline.ingest_content", return_value=mock_result),
        ):
            resp = tc.post(
                "/v1/capture",
                json={
                    "content": "hello",
                    "session_id": "abcd1234ef",
                    "cwd": str(tmp_path),
                },
            )

        assert resp.status_code == 202
        assert resp.json()["task_id"].startswith("capture-")

    def test_missing_content_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/capture", json={"session_id": "abcd1234"})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_missing_name_and_session_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/capture", json={"content": "hi"})
        assert resp.status_code == 400
        error = resp.json()["error"].lower()
        assert "document_name" in error or "session" in error

    def test_whitespace_name_and_session_returns_400(self, client: TestClient) -> None:
        """Whitespace-only name/session must earn the 400, not a blank-named doc."""
        resp = client.post(
            "/v1/capture",
            json={"content": "hi", "document_name": "   ", "session_id": "  \t"},
        )
        assert resp.status_code == 400
        error = resp.json()["error"].lower()
        assert "document_name" in error or "session" in error

    def test_capture_scrubs_and_derives_default_collection(
        self, tmp_path: Path
    ) -> None:
        """The route hands ingest_content a redacting scrubber for default-captures.

        The working directory is unregistered, so the derived collection is
        ``default-captures``; the captured ``content_scrubber`` redacts PII —
        together these prove a stored capture chunk is scrubbed into the right
        collection.  ``ingest_content``'s own scrub-of-stored-pages is covered in
        ``tests/test_pipeline.py::TestIngestContentScrubbing``.
        """
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        scrubbers: list[Callable[[str], str]] = []
        collections: list[object] = []

        def _spy(*_a: object, **kwargs: object) -> dict[str, object]:
            scrubbers.append(cast("Callable[[str], str]", kwargs["content_scrubber"]))
            collections.append(kwargs["collection"])
            return {
                "document_name": "note",
                "collection": "default-captures",
                "chunks": 0,
            }

        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.ingestion.pipeline.ingest_content", _spy),
        ):
            resp = tc.post(
                "/v1/capture",
                json={
                    "content": "reach me at jmf@pobox.com",
                    "document_name": "note",
                    "cwd": str(tmp_path),
                },
            )
            _poll_task_done(tc, resp.json()["task_id"])

        assert collections == ["default-captures"]
        assert scrubbers
        redacted = scrubbers[0]("reach me at jmf@pobox.com")
        assert "jmf@pobox.com" not in redacted
        assert "[REDACTED:email]" in redacted

    def test_scrub_failure_marks_task_failed_and_stores_nothing(
        self, tmp_path: Path
    ) -> None:
        """A scrub that raises fails the task before any chunk is stored."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.scrub.scrub_and_log",
                side_effect=ValueError("scrub exploded"),
            ),
            patch("quarry.ingestion.pipeline._chunk_embed_store") as store,
            patch("quarry.db.chunk_store.ChunkStore.delete_document") as delete,
        ):
            resp = tc.post(
                "/v1/capture",
                json={
                    "content": "secret jmf@pobox.com",
                    "document_name": "note",
                    "cwd": str(tmp_path),
                    "overwrite": True,
                },
            )
            data = _poll_task_done(tc, resp.json()["task_id"])

        assert data["status"] == "failed"
        assert "scrub exploded" in data["error"]
        store.assert_not_called()  # nothing stored
        delete.assert_not_called()  # AND the prior document is not deleted

    def test_empty_extraction_refetches_source_url(self, tmp_path: Path) -> None:
        """A JS-rendered page that extracts to zero chunks re-fetches its source.

        The inline HTML yields no chunks; rather than silently index nothing the
        daemon re-fetches the source URL through the SSRF-checked ingest path,
        scrubbing content AND summary (symmetric with the inline phase).
        """
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        url_kwargs: list[dict[str, object]] = []

        def _url(source: str, *_a: object, **kw: object) -> dict[str, object]:
            url_kwargs.append({"source": source, **kw})
            return {
                "document_name": source,
                "collection": kw["collection"],
                "chunks": 1,
            }

        empty = {"document_name": "p", "collection": "default-captures", "chunks": 0}
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.ingestion.pipeline.ingest_content", return_value=empty),
            patch("quarry.ingestion.pipeline.ingest_url", _url),
        ):
            resp = tc.post(
                "/v1/capture",
                json={
                    "content": "<html><body></body></html>",
                    "document_name": "example.com/p",
                    "source_url": "https://example.com/p",
                    "summary": "see jmf@pobox.com",
                    "format_hint": "html",
                },
            )
            data = _poll_task_done(tc, resp.json()["task_id"])

        assert data["status"] == "completed"
        assert data["results"]["chunks"] == 1  # the re-fetch indexed the page
        assert len(url_kwargs) == 1
        assert url_kwargs[0]["source"] == "https://example.com/p"
        assert url_kwargs[0]["collection"] == "default-captures"
        # The caller forwards the raw summary plus a content_scrubber; ingest_url
        # (the choke point) redacts summary+name — see test_pipeline's
        # ingest_url metadata-scrub test.  Here we assert the scrubber is wired.
        scrub = cast("Callable[[str], str]", url_kwargs[0]["content_scrubber"])
        assert "[REDACTED:email]" in scrub("reach jmf@pobox.com")

    def test_nonempty_extraction_does_not_refetch(self, tmp_path: Path) -> None:
        """A page that extracts to >=1 chunk stores inline and never re-fetches."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        stored = {"document_name": "p", "collection": "default-captures", "chunks": 3}
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.ingestion.pipeline.ingest_content", return_value=stored),
            patch("quarry.ingestion.pipeline.ingest_url") as url,
        ):
            resp = tc.post(
                "/v1/capture",
                json={
                    "content": "real page text",
                    "document_name": "example.com/p",
                    "source_url": "https://example.com/p",
                    "format_hint": "html",
                },
            )
            data = _poll_task_done(tc, resp.json()["task_id"])

        assert data["status"] == "completed"
        assert data["results"]["chunks"] == 3
        url.assert_not_called()  # inline chunks were stored — no re-fetch

    @pytest.mark.parametrize(
        "source_url",
        [
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://metadata.google.internal/",  # GCP metadata host
            "http://127.0.0.1/admin",  # loopback
            "http://10.0.0.1/",  # RFC 1918 private
            "http://192.168.1.1/",  # RFC 1918 private
        ],
    )
    def test_internal_source_url_rejected_and_never_fetched(
        self, tmp_path: Path, source_url: str
    ) -> None:
        """An internal source_url is rejected at the route, never fetched.

        The daemon re-fetches source_url server-side when inline HTML extracts to
        zero chunks — an SSRF sink.  The capture route must run the same
        UrlSafetyCheck gate as /v1/ingest, fail-closed with a 400, before any
        job runs, so a crafted internal URL never reaches ingest_url/WebFetcher.
        """
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        empty = {"document_name": "p", "collection": "default-captures", "chunks": 0}
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.ingestion.pipeline.ingest_content", return_value=empty
            ) as content,
            patch("quarry.ingestion.pipeline.ingest_url") as url,
        ):
            resp = tc.post(
                "/v1/capture",
                json={
                    "content": "<html><body></body></html>",
                    "document_name": "victim",
                    "source_url": source_url,
                    "format_hint": "html",
                },
            )

        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()
        url.assert_not_called()  # the SSRF sink was never reached
        content.assert_not_called()  # rejected before any ingest job ran


class TestRemember:
    """Tests for POST /remember endpoint -- now returns 202."""

    def test_success_returns_202(self, tmp_path: Path) -> None:
        mock_result = {
            "document_name": "notes.md",
            "collection": "default",
            "chunks": 3,
        }
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.ingestion.pipeline.ingest_content", return_value=mock_result),
        ):
            resp = tc.post(
                "/v1/remember",
                json={"name": "notes.md", "content": "hello world"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("remember-")

    def test_missing_content_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/remember", json={"name": "notes.md"})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_missing_name_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/remember", json={"content": "hello"})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_empty_content_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/remember", json={"name": "a.md", "content": ""})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_whitespace_content_returns_400(self, client: TestClient) -> None:
        """Whitespace-only content must be rejected (matches local CLI)."""
        resp = client.post(
            "/v1/remember", json={"name": "a.md", "content": "   \n\n\t"}
        )
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_whitespace_name_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/remember", json={"name": "   ", "content": "hello"})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_scrubs_name_and_summary_not_just_content(self, tmp_path: Path) -> None:
        """A secret in the free-form name or summary is redacted before store.

        The daemon forwards the raw name/summary and a content_scrubber; the
        pipeline choke point (ingest_content) is what redacts them, so this
        patches the store boundary and asserts the REAL scrub ran end-to-end —
        the chunker copies name+summary onto every chunk, so content-only
        scrubbing would leak."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        seen: dict[str, object] = {}

        def _store(
            _pages: object, document_name: str, *_a: object, **kw: object
        ) -> dict[str, object]:
            seen["name"] = document_name
            seen["summary"] = kw["summary"]
            return {"document_name": document_name, "collection": "c", "chunks": 0}

        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.ingestion.pipeline._chunk_embed_store", _store),
        ):
            resp = tc.post(
                "/v1/remember",
                json={
                    "name": "note jmf@pobox.com",
                    "content": "body",
                    "summary": "contact jmf@pobox.com",
                },
            )
            _poll_task_done(tc, resp.json()["task_id"])

        assert "jmf@pobox.com" not in str(seen["name"])
        assert "[REDACTED:email]" in str(seen["name"])
        assert "jmf@pobox.com" not in str(seen["summary"])
        assert "[REDACTED:email]" in str(seen["summary"])

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/remember",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_pipeline_value_error_marks_task_failed(self, tmp_path: Path) -> None:
        """ingest_content raising ValueError marks the task as failed."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.ingestion.pipeline.ingest_content",
                side_effect=ValueError("bad content encoding"),
            ),
        ):
            resp = tc.post(
                "/v1/remember",
                json={"name": "n.md", "content": "body"},
            )
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["status"] == "failed"
        assert "bad content encoding" in data["error"]

    def test_pipeline_os_error_marks_task_failed(self, tmp_path: Path) -> None:
        """ingest_content raising OSError marks the task as failed."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.ingestion.pipeline.ingest_content",
                side_effect=OSError("disk full"),
            ),
        ):
            resp = tc.post(
                "/v1/remember",
                json={"name": "n.md", "content": "body"},
            )
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["status"] == "failed"
        assert "disk full" in data["error"]

    def test_rejects_oversized_body(self, client: TestClient) -> None:
        """Remember body > 50 MB must be rejected with HTTP 413."""
        from quarry.daemon.routes.ingestion import MAX_REMEMBER_BODY_BYTES

        too_big = MAX_REMEMBER_BODY_BYTES + 1
        resp = client.post(
            "/v1/remember",
            content=b"x",  # actual body tiny; Content-Length header lies
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(too_big),
            },
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["error"].lower()

    def test_passes_all_params(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            patch(
                "quarry.ingestion.pipeline.ingest_content",
                return_value={"document_name": "n", "collection": "c", "chunks": 1},
            ) as mock_ingest,
            TestClient(app, raise_server_exceptions=False) as tc,
        ):
            resp = tc.post(
                "/v1/remember",
                json={
                    "name": "n.md",
                    "content": "body",
                    "collection": "notes",
                    "format_hint": "markdown",
                    "overwrite": False,
                    "agent_handle": "rmh",
                    "memory_type": "fact",
                    "summary": "one line",
                },
            )
            _poll_task_done(tc, resp.json()["task_id"])

        assert mock_ingest.call_count == 1
        args, kwargs = mock_ingest.call_args
        # Positional: content, name, db, settings
        assert args[0] == "body"
        assert args[1] == "n.md"
        assert kwargs["collection"] == "notes"
        assert kwargs["format_hint"] == "markdown"
        assert kwargs["overwrite"] is False
        assert kwargs["agent_handle"] == "rmh"
        assert kwargs["memory_type"] == "fact"
        assert kwargs["summary"] == "one line"

    def test_overwrite_defaults_true(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            patch(
                "quarry.ingestion.pipeline.ingest_content",
                return_value={"document_name": "n", "collection": "c", "chunks": 1},
            ) as mock_ingest,
            TestClient(app, raise_server_exceptions=False) as tc,
        ):
            resp = tc.post(
                "/v1/remember",
                json={"name": "n.md", "content": "body"},
            )
            _poll_task_done(tc, resp.json()["task_id"])
            assert mock_ingest.call_args is not None
            assert mock_ingest.call_args.kwargs["overwrite"] is True

    def test_rejects_non_bool_overwrite(self, client: TestClient) -> None:
        """Strings like 'false' or '0' must not be silently coerced to True."""
        resp = client.post(
            "/v1/remember",
            json={"name": "n.md", "content": "body", "overwrite": "false"},
        )
        assert resp.status_code == 400
        assert "overwrite" in resp.json()["error"].lower()
        assert "boolean" in resp.json()["error"].lower()

    def test_rejects_integer_overwrite(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/remember",
            json={"name": "n.md", "content": "body", "overwrite": 0},
        )
        assert resp.status_code == 400
        assert "overwrite" in resp.json()["error"].lower()


def _fake_public_addrinfo(
    _host: str,
    *_args: object,
    **_kwargs: object,
) -> list[tuple[object, object, object, str, tuple[str, int]]]:
    """Stand in for socket.getaddrinfo() -- resolves every host to 93.184.216.34."""
    return [(None, None, None, "", ("93.184.216.34", 0))]


class TestIngest:
    """Tests for POST /ingest endpoint."""

    def test_success_with_url(self, tmp_path: Path) -> None:
        mock_result = {
            "document_name": "https://example.com",
            "collection": "example.com",
            "chunks": 5,
        }
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch("quarry.ingestion.pipeline.ingest_auto", return_value=mock_result),
        ):
            resp = tc.post("/v1/ingest", json={"source": "https://example.com/docs"})
            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "accepted"
            assert data["task_id"].startswith("ingest-")
            # Drain under the mock so the worker never runs the real ingest_auto.
            _poll_task_done(tc, data["task_id"])

    def test_scrub_capture_with_empty_cwd_scrubs_not_ingest_auto(
        self, tmp_path: Path
    ) -> None:
        """A web-fetch capture (scrub=True) with EMPTY cwd must scrub and land in
        default-captures via ingest_url — never the unscrubbed ingest_auto branch.

        The empty-cwd -> default-captures derivation is unit-tested in
        ``test_captures_collection``; here the collection resolver is stubbed so
        the assertion isolates the daemon's scrub-vs-plain routing.
        """
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        url_kwargs: list[dict[str, object]] = []

        def _url(*_a: object, **kw: object) -> dict[str, object]:
            url_kwargs.append(kw)
            return {"document_name": "u", "collection": kw["collection"], "chunks": 1}

        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.captures_collection.CapturesCollection.for_registry_path",
                return_value=CapturesCollection.fallback(),
            ),
            patch("quarry.ingestion.pipeline.ingest_url", _url),
            patch("quarry.ingestion.pipeline.ingest_auto") as auto,
        ):
            resp = tc.post(
                "/v1/ingest",
                json={
                    "source": "https://example.com/p",
                    "scrub": True,
                    "cwd": "",
                    "overwrite": True,
                },
            )
            _poll_task_done(tc, resp.json()["task_id"])

        auto.assert_not_called()  # never the unscrubbed sitemap branch
        assert url_kwargs
        assert url_kwargs[0]["collection"] == "default-captures"
        scrub = cast("Callable[[str], str]", url_kwargs[0]["content_scrubber"])
        assert "[REDACTED:email]" in scrub("reach me at jmf@pobox.com")

    def test_missing_source_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/ingest", json={})
        assert resp.status_code == 400
        assert "source" in resp.json()["error"].lower()

    def test_non_url_source_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/ingest", json={"source": "/path/to/file.pdf"})
        assert resp.status_code == 400
        assert "url" in resp.json()["error"].lower()

    def test_empty_source_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/ingest", json={"source": ""})
        assert resp.status_code == 400

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/ingest",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_passes_all_params(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.ingestion.pipeline.ingest_auto",
                return_value={"document_name": "d", "collection": "c", "chunks": 1},
            ) as mock_ingest,
            TestClient(app, raise_server_exceptions=False) as tc,
        ):
            resp = tc.post(
                "/v1/ingest",
                json={
                    "source": "https://example.com/docs",
                    "overwrite": True,
                    "collection": "mycol",
                    "agent_handle": "rmh",
                    "memory_type": "fact",
                    "summary": "one line",
                },
            )
            _poll_task_done(tc, resp.json()["task_id"])

        assert resp.status_code == 202
        assert mock_ingest.call_count == 1
        args, kwargs = mock_ingest.call_args
        assert args[0] == "https://example.com/docs"
        assert kwargs["overwrite"] is True
        assert kwargs["collection"] == "mycol"
        assert kwargs["agent_handle"] == "rmh"
        assert kwargs["memory_type"] == "fact"
        assert kwargs["summary"] == "one line"

    def test_rejects_private_ip(self, client: TestClient) -> None:
        """URLs whose host resolves to RFC 1918 space must be blocked."""

        def fake_getaddrinfo(
            _host: str,
            *_a: object,
            **_kw: object,
        ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
            return [(None, None, None, "", ("192.168.1.1", 0))]

        with patch(
            "quarry.url_safety.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/v1/ingest", json={"source": "http://192.168.1.1/"})
        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()

    def test_rejects_loopback(self, client: TestClient) -> None:
        def fake_getaddrinfo(
            _host: str,
            *_a: object,
            **_kw: object,
        ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
            return [(None, None, None, "", ("127.0.0.1", 0))]

        with patch(
            "quarry.url_safety.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/v1/ingest", json={"source": "http://127.0.0.1/"})
        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()

    def test_rejects_metadata_ip(self, client: TestClient) -> None:
        """Cloud metadata endpoint must be blocked without even resolving."""
        with patch(
            "quarry.url_safety.socket_module.getaddrinfo",
        ) as mock_resolve:
            resp = client.post(
                "/v1/ingest",
                json={"source": "http://169.254.169.254/latest/meta-data/"},
            )
        assert resp.status_code == 400
        assert "metadata" in resp.json()["error"].lower()
        mock_resolve.assert_not_called()

    def test_rejects_dotlocal(self, client: TestClient) -> None:
        """mDNS .local hostnames must be blocked pre-resolution."""
        with patch(
            "quarry.url_safety.socket_module.getaddrinfo",
        ) as mock_resolve:
            resp = client.post("/v1/ingest", json={"source": "http://myserver.local/"})
        assert resp.status_code == 400
        assert ".local" in resp.json()["error"]
        mock_resolve.assert_not_called()

    def test_rejects_link_local(self, client: TestClient) -> None:
        def fake_getaddrinfo(
            _host: str,
            *_a: object,
            **_kw: object,
        ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
            return [(None, None, None, "", ("169.254.10.5", 0))]

        with patch(
            "quarry.url_safety.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post(
                "/v1/ingest", json={"source": "http://autoconfigured.example/"}
            )
        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()

    def test_pipeline_value_error_marks_task_failed(self, tmp_path: Path) -> None:
        """ingest_auto raising ValueError marks the task as failed."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.ingestion.pipeline.ingest_auto",
                side_effect=ValueError("unsupported URL"),
            ),
        ):
            resp = tc.post("/v1/ingest", json={"source": "https://example.com/"})
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["status"] == "failed"
        assert "unsupported URL" in data["error"]

    def test_pipeline_os_error_marks_task_failed(self, tmp_path: Path) -> None:
        """ingest_auto raising OSError marks the task as failed."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.ingestion.pipeline.ingest_auto",
                side_effect=OSError("upstream refused connection"),
            ),
        ):
            resp = tc.post("/v1/ingest", json={"source": "https://example.com/"})
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["status"] == "failed"
        assert "upstream refused connection" in data["error"]

    def test_rejects_oversized_body(self, client: TestClient) -> None:
        """Ingest body > 1 MB must be rejected with HTTP 413."""
        from quarry.daemon.routes.ingestion import MAX_INGEST_BODY_BYTES

        too_big = MAX_INGEST_BODY_BYTES + 1
        resp = client.post(
            "/v1/ingest",
            content=b"x",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(too_big),
            },
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["error"].lower()

    def test_rejects_non_bool_overwrite(self, client: TestClient) -> None:
        """Strings like 'false' must not be silently coerced to True."""
        resp = client.post(
            "/v1/ingest",
            json={"source": "https://example.com/", "overwrite": "false"},
        )
        assert resp.status_code == 400
        assert "overwrite" in resp.json()["error"].lower()
        assert "boolean" in resp.json()["error"].lower()

    def test_accepts_uppercase_scheme(self, tmp_path: Path) -> None:
        """HTTPS:// (uppercase) must be accepted -- scheme is case-insensitive."""
        mock_result = {
            "document_name": "https://example.com",
            "collection": "example.com",
            "chunks": 1,
        }
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch("quarry.ingestion.pipeline.ingest_auto", return_value=mock_result),
        ):
            resp = tc.post("/v1/ingest", json={"source": "HTTPS://example.com/docs"})
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            assert task_id.startswith("ingest-")
            # Drain under the mock so the worker never runs the real ingest_auto.
            _poll_task_done(tc, task_id)

    def test_rejects_cgnat(self, client: TestClient) -> None:
        """RFC 6598 CGNAT addresses (100.64.0.0/10) must be blocked."""

        def fake_getaddrinfo(
            _host: str,
            *_a: object,
            **_kw: object,
        ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
            return [(None, None, None, "", ("100.64.1.1", 0))]

        with patch(
            "quarry.url_safety.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/v1/ingest", json={"source": "http://cgnat.example/"})
        assert resp.status_code == 400
        assert "cgnat" in resp.json()["error"].lower()


class TestSync:
    """Tests for POST /v1/sync and its GET /v1/tasks/<task_id> polling."""

    def test_returns_202_with_task_id(self, tmp_path: Path) -> None:
        """POST /sync returns 202 Accepted with a task_id."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.sync.sync_all", return_value={}),
        ):
            resp = tc.post("/v1/sync", json={})

        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "accepted"

    def test_empty_body_accepted(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.sync.sync_all", return_value={}),
        ):
            resp = tc.post(
                "/v1/sync",
                content=b"",
                headers={"Content-Length": "0", "Content-Type": "application/json"},
            )
        assert resp.status_code == 202

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/sync",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_non_object_body_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v1/sync", json=[1, 2, 3])
        assert resp.status_code == 400

    def test_auth_required(self, auth_client: TestClient) -> None:
        resp = auth_client.post("/v1/sync", json={})
        assert resp.status_code == 401

    def test_auth_allows_with_key(self, auth_client: TestClient) -> None:
        with patch("quarry.sync.sync_all", return_value={}):
            resp = auth_client.post(
                "/v1/sync",
                json={},
                headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
            )
        assert resp.status_code == 202

    def test_rejects_oversized_body(self, client: TestClient) -> None:
        from quarry.daemon.routes.sync import MAX_SYNC_BODY_BYTES

        too_big = MAX_SYNC_BODY_BYTES + 1
        resp = client.post(
            "/v1/sync",
            content=b"x",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(too_big),
            },
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["error"].lower()

    def test_concurrent_sync_enqueues_not_409(self, tmp_path: Path) -> None:
        """A second sync while one runs now enqueues (202), never 409 (DES-045).

        With the watch loop always active a 409 would reject every explicit
        sync, so the request enqueues behind the live work and returns 202 + a
        fresh task_id; the per-collection FIFO queue is the concurrency control.
        """
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        sync_client = TestClient(app, raise_server_exceptions=False)

        # Simulate an in-progress sync task via the unified tasks dict.
        task_id = "sync-test123"
        ctx.tasks.seed(TaskState(task_id=task_id, kind="sync", status="running"))

        resp = sync_client.post("/v1/sync", json={})
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        assert resp.json()["task_id"] != task_id  # a distinct, freshly-enqueued task

    def test_sync_status_not_found(self, client: TestClient) -> None:
        """GET /v1/tasks/<task_id> returns 404 for unknown task."""
        resp = client.get("/v1/tasks/nonexistent-id")
        assert resp.status_code == 404

    def test_sync_status_completed(self, tmp_path: Path) -> None:
        """GET /v1/tasks/<task_id> returns a completed sync's state."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)

        # Simulate a completed sync task.
        ctx.tasks.seed(
            TaskState(
                task_id="sync-test-123",
                kind="sync",
                status="completed",
                results={"math": {"ingested": 3}},
            )
        )

        app = build_app(ctx)
        sync_client = TestClient(app, raise_server_exceptions=False)

        resp = sync_client.get("/v1/tasks/sync-test-123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "sync-test-123"
        assert data["status"] == "completed"
        assert data["results"]["math"]["ingested"] == 3


class TestDatabases:
    """Tests for GET /databases endpoint."""

    def test_returns_single_entry_list(self, client: TestClient) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            return_value=[{"x": 1}],
        ):
            resp = client.get("/v1/databases")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_databases"] == 1
        assert len(data["databases"]) == 1
        entry = data["databases"][0]
        assert set(entry.keys()) == {
            "name",
            "document_count",
            "size_bytes",
            "size_description",
        }
        assert entry["document_count"] == 1

    def test_name_from_parent_dir(self, tmp_path: Path) -> None:
        """Database name should come from the lancedb parent directory name."""
        settings = _mock_settings(tmp_path)
        # Override so lancedb_path lives under 'work/lancedb'.
        work_dir = tmp_path / "work" / "lancedb"
        work_dir.mkdir(parents=True)
        settings.lancedb_path = work_dir
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ):
            data = client.get("/v1/databases").json()

        assert data["databases"][0]["name"] == "work"

    def test_auth_required(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/v1/databases")
        assert resp.status_code == 401


class TestUse:
    """Tests for POST /use endpoint."""

    def test_returns_400_not_supported(self, client: TestClient) -> None:
        resp = client.post("/v1/use", json={"name": "work"})
        assert resp.status_code == 400
        error = resp.json()["error"].lower()
        assert "client-side" in error

    def test_auth_required(self, auth_client: TestClient) -> None:
        resp = auth_client.post("/v1/use", json={"name": "work"})
        assert resp.status_code == 401


class TestRegistrations:
    """Tests for GET/POST/DELETE /registrations endpoint."""

    @pytest.fixture()
    def home_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        """A client whose settings live under ``tmp_path``."""
        home = tmp_path / "home"
        home.mkdir()
        settings = _mock_settings(tmp_path)
        settings.registry_path = home / "registry.db"
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        resolved = tmp_path.resolve()
        monkeypatch.setattr(
            "quarry.daemon.routes.registrations.RegistrationRoutes._server_home",
            lambda: (resolved, None),
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_get_empty_when_no_registry(self, client: TestClient) -> None:
        resp = client.get("/v1/registrations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_registrations"] == 0
        assert data["registrations"] == []

    def test_get_lists_registrations(self, client: TestClient) -> None:
        from quarry.sync_registry import DirectoryRegistration

        regs = [
            DirectoryRegistration(
                directory="/home/u/math",
                collection="math",
                registered_at="2026-01-01T00:00:00",
            )
        ]
        with (
            patch("quarry.daemon.routes.registrations.SyncRegistry") as mock_registry,
            patch(
                "pathlib.Path.exists",
                return_value=True,
            ),
        ):
            mock_registry.return_value.list_registrations.return_value = regs
            data = client.get("/v1/registrations").json()

        assert data["total_registrations"] == 1
        entry = data["registrations"][0]
        assert entry["collection"] == "math"
        assert entry["directory"] == "/home/u/math"
        assert entry["registered_at"] == "2026-01-01T00:00:00"

    def test_post_registers_directory_returns_202(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST /registrations returns 202 with task_id."""
        home = tmp_path / "home"
        home.mkdir()
        target = home / "docs"
        target.mkdir()
        settings = _mock_settings(tmp_path)
        settings.registry_path = home / "registry.db"
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        resolved = tmp_path.resolve()
        monkeypatch.setattr(
            "quarry.daemon.routes.registrations.RegistrationRoutes._server_home",
            lambda: (resolved, None),
        )
        monkeypatch.setenv("HOME", str(home))

        with TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post(
                "/v1/registrations",
                json={"directory": str(target), "collection": "docs"},
            )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("register-")

    def test_post_rejects_missing_directory(self, client: TestClient) -> None:
        resp = client.post("/v1/registrations", json={"collection": "c"})
        assert resp.status_code == 400
        assert "directory" in resp.json()["error"].lower()

    def test_post_rejects_missing_collection(self, client: TestClient) -> None:
        resp = client.post("/v1/registrations", json={"directory": "/home/x"})
        assert resp.status_code == 400
        assert "collection" in resp.json()["error"].lower()

    def test_post_rejects_path_with_parent_segment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        resp = client.post(
            "/v1/registrations",
            json={"directory": "/home/../etc", "collection": "c"},
        )
        assert resp.status_code == 400
        assert ".." in resp.json()["error"]

    def test_post_rejects_outside_home(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        resp = client.post(
            "/v1/registrations",
            json={"directory": "/etc", "collection": "c"},
        )
        assert resp.status_code == 400
        assert "outside" in resp.json()["error"].lower()

    def test_post_rejects_nonexistent_path(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        missing = home / "does-not-exist"
        resp = home_client.post(
            "/v1/registrations",
            json={"directory": str(missing), "collection": "c"},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"].lower()

    def test_delete_returns_202(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DELETE /registrations returns 202 with task_id."""
        home = tmp_path / "home"
        home.mkdir()
        settings = _mock_settings(tmp_path)
        settings.registry_path = home / "registry.db"
        # Create registry so it exists.
        settings.registry_path.touch()
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        resolved = tmp_path.resolve()
        monkeypatch.setattr(
            "quarry.daemon.routes.registrations.RegistrationRoutes._server_home",
            lambda: (resolved, None),
        )

        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.db.chunk_store.ChunkStore.delete_document", return_value=0),
            patch(
                "quarry.daemon.routes.registrations.RegistrationRoutes._deregister_sync",
                return_value=(True, ["a.pdf"]),
            ),
        ):
            resp = tc.delete("/v1/registrations?collection=docs")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("deregister-")
        assert data["removed"] == 1

    def test_delete_nonexistent_returns_404_no_task(self, tmp_path: Path) -> None:
        """Unknown collection -> 404 parity message, no task registered."""
        settings = _mock_settings(tmp_path)
        settings.registry_path = tmp_path / "registry.db"
        settings.registry_path.touch()
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch(
                "quarry.daemon.routes.registrations.RegistrationRoutes._deregister_sync",
                return_value=(False, []),
            ),
        ):
            resp = tc.delete("/v1/registrations?collection=docs")
        assert resp.status_code == 404
        assert resp.json()["error"] == "No registration found for 'docs'"
        assert len(ctx.tasks) == 0

    def test_delete_registry_failure_returns_500_no_task(self, tmp_path: Path) -> None:
        """A synchronous registry failure -> 500, never 202; no task to poll."""
        settings = _mock_settings(tmp_path)
        settings.registry_path = tmp_path / "registry.db"
        settings.registry_path.touch()
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch(
                "quarry.daemon.routes.registrations.RegistrationRoutes._deregister_sync",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
        ):
            resp = tc.delete("/v1/registrations?collection=docs")
        assert resp.status_code == 500
        assert resp.json()["error"]
        assert resp.json().get("status") != "accepted"
        assert len(ctx.tasks) == 0

    def test_delete_keep_data_precompletes_task(self, tmp_path: Path) -> None:
        """keep_data=true -> 202 with a task already completed, zero chunks."""
        settings = _mock_settings(tmp_path)
        settings.registry_path = tmp_path / "registry.db"
        settings.registry_path.touch()
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch(
                "quarry.daemon.routes.registrations.RegistrationRoutes._deregister_sync",
                return_value=(True, ["a.pdf"]),
            ),
        ):
            resp = tc.delete("/v1/registrations?collection=docs&keep_data=true")
            assert resp.status_code == 202
            final = _poll_task_done(tc, resp.json()["task_id"])
        assert final["status"] == "completed"
        assert final["results"]["deleted_chunks"] == 0
        assert final["results"]["removed"] == 1

    def test_delete_rejects_invalid_keep_data(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        resp = home_client.delete("/v1/registrations?collection=docs&keep_data=yes")
        assert resp.status_code == 400
        assert "keep_data" in resp.json()["error"]

    def test_delete_missing_collection_param(self, client: TestClient) -> None:
        resp = client.delete("/v1/registrations")
        assert resp.status_code == 400

    def test_delete_no_registry_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/v1/registrations?collection=anything")
        assert resp.status_code == 404

    def test_auth_required_get(self, auth_client: TestClient) -> None:
        assert auth_client.get("/v1/registrations").status_code == 401

    def test_auth_required_post(self, auth_client: TestClient) -> None:
        assert (
            auth_client.post(
                "/v1/registrations",
                json={"directory": "/home/x", "collection": "c"},
            ).status_code
            == 401
        )

    def test_auth_required_delete(self, auth_client: TestClient) -> None:
        assert auth_client.delete("/v1/registrations?collection=c").status_code == 401

    def test_post_rejects_oversized_body(self, client: TestClient) -> None:
        from quarry.daemon.routes.registrations import MAX_REGISTRATIONS_BODY_BYTES

        too_big = MAX_REGISTRATIONS_BODY_BYTES + 1
        resp = client.post(
            "/v1/registrations",
            content=b"x",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(too_big),
            },
        )
        assert resp.status_code == 413


class TestCheckBodySize:
    """Unit tests for the _check_body_size helper."""

    def test_accepts_body_within_limit(self) -> None:
        from quarry.http_guards import RequestGuards

        request = MagicMock()
        request.headers = {"content-length": "100"}
        assert RequestGuards.check_body_size(request, 200) is None

    def test_rejects_oversized(self) -> None:
        from quarry.http_guards import RequestGuards

        request = MagicMock()
        request.headers = {"content-length": "300"}
        resp = RequestGuards.check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 413

    def test_rejects_missing_content_length(self) -> None:
        from quarry.http_guards import RequestGuards

        request = MagicMock()
        request.headers = {}
        resp = RequestGuards.check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 411

    def test_rejects_non_numeric_content_length(self) -> None:
        from quarry.http_guards import RequestGuards

        request = MagicMock()
        request.headers = {"content-length": "not-a-number"}
        resp = RequestGuards.check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 400

    def test_rejects_negative_content_length(self) -> None:
        from quarry.http_guards import RequestGuards

        request = MagicMock()
        request.headers = {"content-length": "-10"}
        resp = RequestGuards.check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 400


class TestCoerceIntField:
    """Unit tests for the coerce_int_field guard."""

    def test_absent_key_returns_default(self) -> None:
        from quarry.http_guards import RequestGuards

        assert RequestGuards.coerce_int_field({}, "limit", default=7) == 7

    def test_valid_int_returned(self) -> None:
        from quarry.http_guards import RequestGuards

        assert RequestGuards.coerce_int_field({"limit": 5}, "limit", default=0) == 5

    def test_bool_rejected_as_non_int(self) -> None:
        from starlette.responses import JSONResponse

        from quarry.http_guards import RequestGuards

        resp = RequestGuards.coerce_int_field({"limit": True}, "limit", default=0)
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 400

    def test_non_int_rejected(self) -> None:
        from starlette.responses import JSONResponse

        from quarry.http_guards import RequestGuards

        resp = RequestGuards.coerce_int_field({"limit": "lots"}, "limit", default=0)
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 400


class TestServerHomeResolution:
    """Server home is resolved via the passwd database, not ``$HOME``."""

    def test_home_unset_refuses_registration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Registering must fail cleanly when pwd cannot resolve the uid."""
        home = tmp_path / "home"
        home.mkdir()
        settings = _mock_settings(tmp_path)
        settings.registry_path = home / "registry.db"
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        test_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        # Even if an attacker could unset HOME, the server consults pwd.
        monkeypatch.delenv("HOME", raising=False)

        def _boom(_uid: int) -> object:
            raise KeyError("uid not in passwd")

        with patch(
            "quarry.daemon.routes.registrations.pwd.getpwuid", side_effect=_boom
        ):
            resp = test_client.post(
                "/v1/registrations",
                json={"directory": "/etc", "collection": "evil"},
            )

        assert resp.status_code == 400
        assert "home" in resp.json()["error"].lower()

    def test_home_used_is_from_pwd_not_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting HOME to a wider root must not widen the allowlist."""
        real_home = tmp_path / "realhome"
        real_home.mkdir()
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()

        settings = _mock_settings(tmp_path)
        settings.registry_path = real_home / "registry.db"
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        test_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        monkeypatch.setenv("HOME", str(fake_home))
        real_entry = MagicMock()
        real_entry.pw_dir = str(real_home)

        # Registering somewhere under real_home must succeed even though
        # the env says HOME=fakehome.
        target = real_home / "docs"
        target.mkdir()
        with patch(
            "quarry.daemon.routes.registrations.pwd.getpwuid", return_value=real_entry
        ):
            resp = test_client.post(
                "/v1/registrations",
                json={"directory": str(target), "collection": "docs"},
            )
        assert resp.status_code == 202

        # And registering outside pw_dir fails, even if HOME covers it.
        outside = fake_home / "docs"
        outside.mkdir()
        with patch(
            "quarry.daemon.routes.registrations.pwd.getpwuid", return_value=real_entry
        ):
            resp = test_client.post(
                "/v1/registrations",
                json={"directory": str(outside), "collection": "evil"},
            )
        assert resp.status_code == 400
        assert "outside" in resp.json()["error"].lower()


class TestRunPurgeTask:
    """Direct coroutine tests for the async chunk-purge task."""

    def test_purge_success_sets_completed_with_count(self, tmp_path: Path) -> None:
        from quarry.daemon.routes.registrations import RegistrationRoutes

        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        state = TaskState(task_id="deregister-x", kind="deregister")
        state.results = {"collection": "docs", "removed": 1, "deleted_chunks": 0}
        # DES-045: the purge is a collection-wide delete routed through the queue.
        with patch(
            "quarry.db.chunk_store.ChunkStore.delete_collection", return_value=3
        ):
            asyncio.run(RegistrationRoutes(ctx)._run_purge(state, "docs"))
        assert state.status == "completed"
        assert state.results["deleted_chunks"] == 3
        assert state.results["removed"] == 1

    def test_purge_failure_sets_failed(self, tmp_path: Path) -> None:
        from quarry.daemon.routes.registrations import RegistrationRoutes

        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        state = TaskState(task_id="deregister-y", kind="deregister")
        state.results = {"collection": "docs", "removed": 1, "deleted_chunks": 0}
        with patch(
            "quarry.db.chunk_store.ChunkStore.delete_collection",
            side_effect=RuntimeError("purge boom"),
        ):
            asyncio.run(RegistrationRoutes(ctx)._run_purge(state, "docs"))
        assert state.status == "failed"
        assert "purge boom" in state.error


class TestDatabasesMissingTable:
    """Fresh databases (no chunks table) must still respond to /databases."""

    def test_list_documents_error_surfaces_500(self, client: TestClient) -> None:
        """A genuine list_documents error surfaces as 500, not masked to zero.

        A fresh DB (absent chunks table) already yields ``[]`` from
        list_documents, so document_count is naturally 0 with no masking; a
        real catalog error is a server error, not "zero documents".
        """
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            side_effect=RuntimeError("catalog exploded"),
        ):
            resp = client.get("/v1/databases")

        assert resp.status_code == 500
        assert resp.json() == {"error": "Internal server error"}


class TestSyncGenericFailure:
    """Background sync errors are captured in task state, not in the POST response."""

    def test_sync_failure_captured_in_task_state(self, tmp_path: Path) -> None:
        """GET /v1/tasks/<task_id> returns failed state with error message."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)

        # Simulate a failed sync task.
        ctx.tasks.seed(
            TaskState(
                task_id="sync-fail-456",
                kind="sync",
                status="failed",
                error="embedder crashed",
            )
        )

        app = build_app(ctx)
        sync_client = TestClient(app, raise_server_exceptions=False)

        resp = sync_client.get("/v1/tasks/sync-fail-456")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert "embedder crashed" in data["error"]

    def test_remember_runtime_error_marks_task_failed(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.ingestion.pipeline.ingest_content",
                side_effect=RuntimeError("embedder crashed"),
            ),
        ):
            resp = tc.post(
                "/v1/remember",
                json={"name": "n.md", "content": "body"},
            )
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["status"] == "failed"
        assert "embedder crashed" in data["error"]

    def test_ingest_runtime_error_marks_task_failed(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.ingestion.pipeline.ingest_auto",
                side_effect=RuntimeError("embedder crashed"),
            ),
        ):
            resp = tc.post(
                "/v1/ingest",
                json={"source": "https://example.com/"},
            )
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["status"] == "failed"
        assert "embedder crashed" in data["error"]


class TestUnifiedTaskPolling:
    """GET /v1/tasks/{id} is the single polling endpoint; the historical
    /sync/{id} and /ingest/{id} aliases are removed (404)."""

    def test_canonical_task_polling(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)

        ctx.tasks.seed(
            TaskState(
                task_id="sync-abc",
                kind="sync",
                status="completed",
                results={"math": {"ingested": 5}},
            )
        )
        ctx.tasks.seed(
            TaskState(
                task_id="ingest-xyz",
                kind="ingest",
                status="failed",
                error="timeout",
            )
        )

        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/v1/tasks/sync-abc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "sync-abc"
        assert data["status"] == "completed"
        assert data["results"]["math"]["ingested"] == 5

        resp = client.get("/v1/tasks/ingest-xyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "ingest-xyz"
        assert data["status"] == "failed"
        assert data["error"] == "timeout"

        # The old task-status aliases are gone — canonical /v1/tasks only.
        assert client.get("/v1/sync/sync-abc").status_code == 404
        assert client.get("/v1/ingest/ingest-xyz").status_code == 404

    def test_task_not_found(self, client: TestClient) -> None:
        resp = client.get("/v1/tasks/nonexistent")
        assert resp.status_code == 404


class TestTaskGC:
    """Task GC evicts completed/failed tasks older than TASK_TTL_SECONDS."""

    def test_expired_tasks_evicted_on_new_task_creation(self, tmp_path: Path) -> None:
        import time

        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)

        # Add an old completed task and an old running task.
        old_time = time.monotonic() - TASK_TTL_SECONDS - 100
        ctx.tasks.seed(
            TaskState(
                task_id="old-completed",
                kind="ingest",
                status="completed",
                created_at=old_time,
            )
        )
        ctx.tasks.seed(
            TaskState(
                task_id="old-running",
                kind="sync",
                status="running",
                created_at=old_time,
            )
        )
        ctx.tasks.seed(
            TaskState(
                task_id="recent-failed",
                kind="remember",
                status="failed",
                created_at=time.monotonic(),
            )
        )

        app = build_app(ctx)
        # Trigger GC by creating a new task via POST /remember.
        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch(
                "quarry.ingestion.pipeline.ingest_content",
                return_value={"chunks": 1},
            ),
        ):
            tc.post("/v1/remember", json={"name": "x.md", "content": "y"})

        # old-completed should be evicted.
        assert "old-completed" not in ctx.tasks
        # old-running should survive (running tasks are never evicted).
        assert "old-running" in ctx.tasks
        # recent-failed should survive (not yet expired).
        assert "recent-failed" in ctx.tasks


class TestMaintenance:
    """POST /v1/optimize and /v1/backfill-sessions run as 202 background tasks."""

    def test_optimize_returns_202_and_completes(self, tmp_path: Path) -> None:
        from quarry.db.optimizer import OptimizeOutcome

        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        ran = OptimizeOutcome(optimized=True, fragments=7)
        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch("quarry.db.optimizer.TableOptimizer.optimize", return_value=ran),
        ):
            resp = tc.post("/v1/optimize", json={})
            assert resp.status_code == 202
            data = _poll_task_done(tc, resp.json()["task_id"])
        assert data["status"] == "completed"
        results = data["results"]
        assert results["optimized"] is True
        assert results["force"] is False
        assert results["fragments_before"] == 7
        assert "reason" not in results

    def test_optimize_reports_skip_not_success(self, tmp_path: Path) -> None:
        """A skipped compaction reports optimized:False + reason, never a false True.

        The bug: the handler hardcoded optimized:True, so a threshold skip (the
        optimizer returning without compacting) was reported as success.
        """
        from quarry.db.optimizer import OptimizeOutcome

        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        skip = OptimizeOutcome(
            optimized=False, fragments=20000, reason="20,000 fragments exceed threshold"
        )
        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch("quarry.db.optimizer.TableOptimizer.optimize", return_value=skip),
        ):
            resp = tc.post("/v1/optimize", json={})
            assert resp.status_code == 202
            data = _poll_task_done(tc, resp.json()["task_id"])
        results = data["results"]
        assert results["optimized"] is False
        assert results["fragments_before"] == 20000
        assert "threshold" in results["reason"].lower()

    def test_optimize_reads_force_param(self, tmp_path: Path) -> None:
        """The ``force`` flag reaches the engine (param -> server-reads-it)."""
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
            task_id = tc.post("/v1/optimize", json={"force": True}).json()["task_id"]
            data = _poll_task_done(tc, task_id)
        assert data["results"]["force"] is True

    def test_optimize_rejects_non_bool_force(self, client: TestClient) -> None:
        resp = client.post("/v1/optimize", json={"force": "yes"})
        assert resp.status_code == 400

    def test_backfill_reads_all_params_and_returns_stats(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        captured: dict[str, object] = {}

        def _fake_backfill(
            _settings: object,
            *,
            dry_run: bool,
            collection_override: str,
            project_filter: str,
            limit: int,
        ) -> BackfillStats:
            captured.update(
                dry_run=dry_run,
                collection=collection_override,
                project=project_filter,
                limit=limit,
            )
            return BackfillStats(ingested=3, skipped_existing=1)

        with (
            TestClient(app, raise_server_exceptions=False) as tc,
            patch("quarry.backfill.backfill_sessions", _fake_backfill),
        ):
            resp = tc.post(
                "/v1/backfill-sessions",
                json={"dry_run": True, "collection": "c", "project": "p", "limit": 5},
            )
            assert resp.status_code == 202
            data = _poll_task_done(tc, resp.json()["task_id"])

        # Every CLI param reached the engine call (bug-class-3: param -> server).
        assert captured == {
            "dry_run": True,
            "collection": "c",
            "project": "p",
            "limit": 5,
        }
        results = data["results"]
        assert results["ingested"] == 3
        assert results["skipped_existing"] == 1
        assert results["dry_run"] is True

    def test_backfill_rejects_non_int_limit(self, client: TestClient) -> None:
        resp = client.post("/v1/backfill-sessions", json={"limit": "lots"})
        assert resp.status_code == 400

    def test_backfill_rejects_non_bool_dry_run(self, client: TestClient) -> None:
        """A non-bool ``dry_run`` is a 400, never a truthy coercion (Class 2)."""
        resp = client.post("/v1/backfill-sessions", json={"dry_run": "yes"})
        assert resp.status_code == 400

    def test_backfill_rejects_non_object_body(self, client: TestClient) -> None:
        """A JSON array body is rejected before any field is read (Class 2)."""
        resp = client.post("/v1/backfill-sessions", json=[1, 2, 3])
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        ("json_body", "headers", "expected"),
        [
            (None, {"Content-Type": "application/json"}, 0),
            ({"limit": 0}, None, 0),
            ({"limit": 5}, None, 5),
            ({"limit": 1_000_000_000}, None, 1_000_000_000),
            ({"limit": -5}, None, None),
        ],
    )
    def test_backfill_limit_is_pagination_not_a_safety_cap(
        self,
        tmp_path: Path,
        json_body: dict[str, int] | None,
        headers: dict[str, str] | None,
        expected: int | None,
    ) -> None:
        """Remote ``limit`` is a pure pagination knob that agrees with local.

        A backfill run is bounded by construction — it streams one transcript at
        a time and does not leak descriptors (proven by
        ``test_large_backfill_does_not_leak_descriptors``) — so no magic-number
        cap stands in for resource safety. ``limit=0``/empty means "all"
        (matching ``backfill_sessions(limit=0)``) and a positive limit passes
        through unchanged with no ceiling; a *negative* limit is rejected at the
        boundary (``expected=None`` → 400, engine never called), never silently
        normalised to "all". This is the remote half of the local/remote
        equivalence (bug class 3): the daemon no longer rewrites the CLI's
        ``limit=0`` default into a 500-transcript cap.
        """
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        captured: dict[str, object] = {}

        def _fake_backfill(
            _settings: object,
            *,
            dry_run: bool,
            collection_override: str,
            project_filter: str,
            limit: int,
        ) -> BackfillStats:
            captured["limit"] = limit
            return BackfillStats()

        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch("quarry.backfill.backfill_sessions", _fake_backfill),
        ):
            resp = tc.post("/v1/backfill-sessions", json=json_body, headers=headers)
            if expected is None:
                assert resp.status_code == 400
                assert captured == {}
                return
            assert resp.status_code == 202
            _poll_task_done(tc, resp.json()["task_id"])

        assert captured["limit"] == expected

    @pytest.mark.parametrize("limit", [0, 7, 5000])
    def test_cli_backfill_request_reaches_engine_unchanged(
        self, tmp_path: Path, limit: int
    ) -> None:
        """A CLI ``BackfillRequest`` reaches ``backfill_sessions`` field-for-field.

        The full local/remote equivalence (bug class 3): the CLI builds a
        ``BackfillRequest`` and the client posts ``model_dump()`` as the body, so
        serializing that exact contract and driving the route must hand
        ``backfill_sessions`` the same values the CLI asked for. This locks both
        the ``0 == all`` limit semantics (no silent rewrite into a cap) AND the
        two field *renames* the route performs — ``collection`` →
        ``collection_override`` and ``project`` → ``project_filter`` — where a
        typo would be a silent divergence the limit-only assertion misses.
        """
        from quarry.api import BackfillRequest

        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        captured: dict[str, object] = {}

        def _fake_backfill(
            _settings: object,
            *,
            dry_run: bool,
            collection_override: str,
            project_filter: str,
            limit: int,
        ) -> BackfillStats:
            captured.update(
                dry_run=dry_run,
                collection_override=collection_override,
                project_filter=project_filter,
                limit=limit,
            )
            return BackfillStats()

        req = BackfillRequest(
            dry_run=True, collection="proj-captures", project="/repo/proj", limit=limit
        )
        with (
            TestClient(build_app(ctx), raise_server_exceptions=False) as tc,
            patch("quarry.backfill.backfill_sessions", _fake_backfill),
        ):
            resp = tc.post("/v1/backfill-sessions", json=req.model_dump())
            assert resp.status_code == 202
            _poll_task_done(tc, resp.json()["task_id"])

        assert captured == {
            "dry_run": True,
            "collection_override": "proj-captures",
            "project_filter": "/repo/proj",
            "limit": limit,
        }

    def test_optimize_accepts_empty_body(self, client: TestClient) -> None:
        """Empty body is accepted (its documented requestBody is optional)."""
        resp = client.post("/v1/optimize", headers={"Content-Type": "application/json"})
        assert resp.status_code == 202

    def test_backfill_accepts_empty_body(self, client: TestClient) -> None:
        """Empty body is accepted (its documented requestBody is optional)."""
        resp = client.post(
            "/v1/backfill-sessions", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 202

    def test_optimize_rejects_non_json_content_type(self, client: TestClient) -> None:
        """A non-JSON Content-Type is rejected with 415 (CSRF simple-request guard)."""
        resp = client.post(
            "/v1/optimize", content=b"{}", headers={"Content-Type": "text/plain"}
        )
        assert resp.status_code == 415

    def test_backfill_rejects_non_json_content_type(self, client: TestClient) -> None:
        """A non-JSON Content-Type is rejected with 415 (CSRF simple-request guard)."""
        resp = client.post(
            "/v1/backfill-sessions",
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 415

    def test_optimize_rejects_absent_content_type(self, client: TestClient) -> None:
        """A bodyless POST with no Content-Type is rejected (simple-request CSRF)."""
        assert client.post("/v1/optimize").status_code == 415

    def test_concurrent_optimize_returns_409(self, tmp_path: Path) -> None:
        """Second optimize while one runs returns 409 with the running task_id."""
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        ctx.tasks.seed(
            TaskState(task_id="optimize-x", kind="optimize", status="running")
        )
        tc = TestClient(build_app(ctx), raise_server_exceptions=False)
        resp = tc.post("/v1/optimize", json={})
        assert resp.status_code == 409
        assert resp.json()["task_id"] == "optimize-x"
        assert "already in progress" in resp.json()["error"].lower()

    def test_concurrent_backfill_returns_409(self, tmp_path: Path) -> None:
        """Second backfill while one runs returns 409 with the running task_id.

        A concurrent backfill would rescan and double-ingest the same sessions,
        so the singleton guard is a correctness lock, not just a courtesy.
        """
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        ctx.tasks.seed(
            TaskState(task_id="backfill-x", kind="backfill", status="running")
        )
        tc = TestClient(build_app(ctx), raise_server_exceptions=False)
        resp = tc.post("/v1/backfill-sessions", json={})
        assert resp.status_code == 409
        assert resp.json()["task_id"] == "backfill-x"
        assert "already in progress" in resp.json()["error"].lower()


class TestContentTypeChokePoint:
    """Every mutating POST is rejected with 415 unless it advertises JSON.

    The guard is app-level middleware, so it covers routes that read no body
    (``/captures/push``) and routes that accept an empty one (sync, optimize,
    backfill) alike — the fail-closed choke point, not a per-route check.
    """

    _MUTATING_POSTS = (
        "/v1/remember",
        "/v1/ingest",
        "/v1/sync",
        "/v1/registrations",
        "/v1/captures/push",
        "/v1/use",
        "/v1/optimize",
        "/v1/backfill-sessions",
    )

    @pytest.mark.parametrize("path", _MUTATING_POSTS)
    def test_non_json_content_type_rejected(
        self, client: TestClient, path: str
    ) -> None:
        resp = client.post(path, content=b"{}", headers={"Content-Type": "text/plain"})
        assert resp.status_code == 415

    @pytest.mark.parametrize("path", _MUTATING_POSTS)
    def test_absent_content_type_rejected(self, client: TestClient, path: str) -> None:
        """A bodyless cross-origin POST (no Content-Type) is a simple request."""
        assert client.post(path).status_code == 415

    def test_openapi_advertises_json_for_every_post(self, tmp_path: Path) -> None:
        """The published contract matches the guard: every POST documents JSON.

        Model-less POSTs (sync, captures/push, use) still advertise an
        application/json requestBody so a client isn't told a bare POST is fine.
        """
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        spec = build_app(ctx).openapi()
        for path, ops in spec["paths"].items():
            if "post" not in ops:
                continue
            content = ops["post"].get("requestBody", {}).get("content", {})
            assert "application/json" in content, path


class TestResponseModelParity:
    """Each response route's JSON keys match its api model — the wire contract.

    This is the bug-class-3 lock: a field added to the model but not emitted by
    the handler (or vice versa) fails here, catching local/remote divergence at
    its source — the daemon and QuarryClient share these exact models.
    """

    def test_health_keys_match_model(self, client: TestClient) -> None:
        assert set(client.get("/health").json()) == set(HealthResponse.model_fields)

    def test_status_keys_match_model(self, client: TestClient) -> None:
        assert set(client.get("/v1/status").json()) == set(StatusResponse.model_fields)

    def test_search_keys_match_model(self, client: TestClient) -> None:
        body = client.get("/v1/search?q=test").json()
        assert set(body) == set(SearchResponse.model_fields)

    def test_show_documents_both_200_shapes(self, tmp_path: Path) -> None:
        """/show returns page text OR document metadata — OpenAPI documents both.

        The RouteSpec once advertised only ShowPageResponse, so the metadata
        case (``page`` omitted -> DocumentInfo) was undocumented.
        """
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        spec = build_app(ctx).openapi()
        schema = spec["paths"]["/v1/show"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        refs = {opt.get("$ref", "") for opt in schema.get("anyOf", [])}
        assert any(r.endswith("/ShowPageResponse") for r in refs), refs
        assert any(r.endswith("/DocumentInfo") for r in refs), refs
