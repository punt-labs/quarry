"""Tests for the quarry HTTP server (quarry serve).

Uses Starlette's TestClient with mocked database and embedding backends.
Each test class gets its own app instance via fixtures.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.api import HealthResponse, SearchResponse, StatusResponse
from quarry.backfill import BackfillStats
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.daemon.server import DaemonServer, PortFile
from quarry.daemon.tasks import TASK_TTL_SECONDS, TaskState
from quarry.results import SearchResult


def _poll_task_done(
    tc: TestClient, task_id: str, max_polls: int = 50
) -> dict[str, Any]:
    """Poll GET /tasks/{task_id} until terminal status. Return the final JSON."""
    data: dict[str, Any] = {}
    for _ in range(max_polls):
        resp = tc.get(f"/v1/tasks/{task_id}")
        data = resp.json()
        if data["status"] != "running":
            return data
    return data


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.registry_path = tmp_path / "registry.db"  # does not exist -> regs = []
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
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
def client(tmp_path: Path) -> TestClient:
    """Build a test app and return a TestClient."""
    settings = _mock_settings(tmp_path)
    ctx = DaemonContext(settings)
    _inject_mocks(ctx)

    app = build_app(ctx)
    return TestClient(app, raise_server_exceptions=False)


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


class TestNotFound:
    def test_unknown_path_returns_404(self, client: TestClient) -> None:
        resp = client.get("/unknown")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Not Found"


class TestPortFile:
    def test_write_port_file(self, tmp_path: Path) -> None:
        port_path = tmp_path / "subdir" / "serve.port"
        PortFile(port_path).write(12345)

        assert port_path.exists()
        assert port_path.read_text() == "12345"

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        port_path = tmp_path / "a" / "b" / "serve.port"
        PortFile(port_path).write(8080)
        assert port_path.exists()


class TestFailClosed:
    """Non-loopback hosts require --api-key."""

    def test_non_loopback_without_key_refuses(self) -> None:
        with pytest.raises(SystemExit, match="Refusing to bind"):
            DaemonServer._validate_host_key("0.0.0.0", None)  # noqa: S104

    def test_loopback_without_key_allowed(self) -> None:
        DaemonServer._validate_host_key("127.0.0.1", None)


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
                "quarry.daemon.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch("quarry.ingestion.pipeline.ingest_auto", return_value=mock_result),
        ):
            resp = tc.post("/v1/ingest", json={"source": "https://example.com/docs"})

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["task_id"].startswith("ingest-")

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
                "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
            "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
            "quarry.daemon.url_safety.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/v1/ingest", json={"source": "http://127.0.0.1/"})
        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()

    def test_rejects_metadata_ip(self, client: TestClient) -> None:
        """Cloud metadata endpoint must be blocked without even resolving."""
        with patch(
            "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
            "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
            "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
                "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
                "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
                "quarry.daemon.url_safety.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch("quarry.ingestion.pipeline.ingest_auto", return_value=mock_result),
        ):
            resp = tc.post("/v1/ingest", json={"source": "HTTPS://example.com/docs"})

        assert resp.status_code == 202
        assert resp.json()["task_id"].startswith("ingest-")

    def test_rejects_cgnat(self, client: TestClient) -> None:
        """RFC 6598 CGNAT addresses (100.64.0.0/10) must be blocked."""

        def fake_getaddrinfo(
            _host: str,
            *_a: object,
            **_kw: object,
        ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
            return [(None, None, None, "", ("100.64.1.1", 0))]

        with patch(
            "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
                headers={"Content-Length": "0"},
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

    def test_concurrent_sync_returns_409(self, tmp_path: Path) -> None:
        """Second POST while sync is running returns 409 with task_id."""
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        app = build_app(ctx)
        sync_client = TestClient(app, raise_server_exceptions=False)

        # Simulate an in-progress sync task via the unified tasks dict.
        task_id = "sync-test123"
        ctx.tasks.seed(TaskState(task_id=task_id, kind="sync", status="running"))

        resp = sync_client.post("/v1/sync", json={})
        assert resp.status_code == 409
        assert resp.json()["task_id"] == task_id
        assert "already in progress" in resp.json()["error"].lower()

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
        with patch("quarry.db.chunk_store.ChunkStore.delete_document", return_value=3):
            asyncio.run(RegistrationRoutes(ctx)._run_purge(state, "docs", ["a.pdf"]))
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
            "quarry.db.chunk_store.ChunkStore.delete_document",
            side_effect=RuntimeError("purge boom"),
        ):
            asyncio.run(RegistrationRoutes(ctx)._run_purge(state, "docs", ["a.pdf"]))
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
                "quarry.daemon.url_safety.socket_module.getaddrinfo",
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
        ctx = DaemonContext(_mock_settings(tmp_path))
        _inject_mocks(ctx)
        with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
            resp = tc.post("/v1/optimize", json={})
            assert resp.status_code == 202
            data = _poll_task_done(tc, resp.json()["task_id"])
        assert data["status"] == "completed"
        results = data["results"]
        assert results["optimized"] is True
        assert results["force"] is False
        assert results["fragments_before"] == 0

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

    def test_optimize_accepts_empty_body(self, client: TestClient) -> None:
        """Empty body is accepted (its documented requestBody is optional)."""
        assert client.post("/v1/optimize").status_code == 202

    def test_backfill_accepts_empty_body(self, client: TestClient) -> None:
        """Empty body is accepted (its documented requestBody is optional)."""
        assert client.post("/v1/backfill-sessions").status_code == 202


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
