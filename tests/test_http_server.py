"""Tests for the quarry HTTP server (quarry serve).

Uses Starlette's TestClient with mocked database and embedding backends.
Each test class gets its own app instance via fixtures.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.http_server import (
    _QuarryContext,
    _validate_host_key,
    _write_port_file,
    build_app,
)


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.registry_path = tmp_path / "registry.db"  # does not exist → regs = []
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    return s


def _mock_embedder() -> MagicMock:
    embedder = MagicMock()
    embedder.embed_query.return_value = np.zeros(768, dtype=np.float32)
    embedder.dimension = 768
    embedder.model_name = "test-model"
    return embedder


def _mock_db() -> MagicMock:
    return MagicMock()


_SHARED_DB = _mock_db()
_SHARED_EMBEDDER = _mock_embedder()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """Build a test app and return a TestClient."""
    settings = _mock_settings(tmp_path)
    ctx = _QuarryContext(settings)
    ctx.__dict__["db"] = _SHARED_DB
    ctx.__dict__["embedder"] = _SHARED_EMBEDDER

    app = build_app(ctx)
    return TestClient(app, raise_server_exceptions=False)


class TestHealth:
    def test_returns_ok(self, client: TestClient) -> None:
        data = client.get("/health").json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0

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
        ctx = _QuarryContext(settings, api_key="secret-key")
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        auth_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        fake_pem = "-----BEGIN CERTIFICATE-----\nfakecert\n-----END CERTIFICATE-----\n"
        (tls_dir / "ca.crt").write_text(fake_pem)

        # No Authorization header — should still get the cert.
        with patch("quarry.tls.TLS_DIR", tls_dir):
            resp = auth_client.get("/ca.crt")
        assert resp.status_code == 200


class TestConcurrency:
    """Verify the server handles concurrent requests without serializing."""

    def test_concurrent_requests_overlap(self, client: TestClient) -> None:
        """Two slow requests should complete in less than 2x a single request."""
        import concurrent.futures
        import time

        delay = 0.3

        def slow_search(*_args: object, **_kwargs: object) -> list[object]:
            time.sleep(delay)
            return []

        with (
            patch("quarry.http_server.hybrid_search", side_effect=slow_search),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            start = time.monotonic()
            futures = [
                pool.submit(lambda: client.get("/search?q=a").json()),
                pool.submit(lambda: client.get("/search?q=b").json()),
            ]
            for f in concurrent.futures.as_completed(futures):
                f.result()
            elapsed = time.monotonic() - start

        assert elapsed < 1.5 * delay, (
            f"Requests appear serialized: {elapsed:.2f}s >= {1.5 * delay:.2f}s"
        )


class TestSearch:
    def test_missing_query_returns_400(self, client: TestClient) -> None:
        assert client.get("/search").status_code == 400

    def test_empty_query_returns_400(self, client: TestClient) -> None:
        assert client.get("/search?q=").status_code == 400

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
        with patch("quarry.http_server.hybrid_search", return_value=mock_results):
            data = client.get("/search?q=hello").json()

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
        with patch("quarry.http_server.hybrid_search", return_value=mock_results):
            data = client.get("/search?q=content").json()

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
        with patch("quarry.http_server.hybrid_search", return_value=mock_results):
            data = client.get("/search?q=content").json()

        assert data["results"][0]["summary"] == ""

    def test_search_with_limit(self, client: TestClient) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&limit=5")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 5

    def test_search_limit_capped_at_50(self, client: TestClient) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&limit=999")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 50

    def test_search_negative_limit_clamped_to_1(self, client: TestClient) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&limit=-5")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 1

    def test_search_with_collection_filter(self, client: TestClient) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&collection=research")

        _, kwargs = mock_search.call_args
        assert kwargs["collection_filter"] == "research"

    def test_search_empty_results(self, client: TestClient) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]):
            data = client.get("/search?q=nonexistent").json()

        assert data["total_results"] == 0
        assert data["results"] == []

    def test_search_agent_handle_filter_passed_through(
        self, client: TestClient
    ) -> None:
        """agent_handle query param must reach hybrid_search as agent_handle_filter."""
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&agent_handle=someagent")

        _, kwargs = mock_search.call_args
        assert kwargs["agent_handle_filter"] == "someagent"

    def test_search_memory_type_filter_passed_through(self, client: TestClient) -> None:
        """memory_type query param must reach hybrid_search as memory_type_filter."""
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&memory_type=episodic")

        _, kwargs = mock_search.call_args
        assert kwargs["memory_type_filter"] == "episodic"

    def test_search_document_filter_passed_through(self, client: TestClient) -> None:
        """document query param must reach hybrid_search as document_filter."""
        with patch("quarry.http_server.hybrid_search", return_value=[]) as mock_search:
            client.get("/search?q=hello&document=report.pdf")

        _, kwargs = mock_search.call_args
        assert kwargs["document_filter"] == "report.pdf"

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
        with patch("quarry.http_server.hybrid_search", return_value=mock_results):
            data = client.get("/search?q=remember").json()

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
        with patch("quarry.http_server.list_documents", return_value=mock_docs):
            data = client.get("/documents").json()

        assert data["total_documents"] == 1
        assert data["documents"][0]["document_name"] == "report.pdf"

    def test_list_documents_with_collection_filter(self, client: TestClient) -> None:
        with patch("quarry.http_server.list_documents", return_value=[]) as mock_list:
            client.get("/documents?collection=research")

        _, kwargs = mock_list.call_args
        assert kwargs["collection_filter"] == "research"


class TestCollections:
    def test_list_collections(self, client: TestClient) -> None:
        mock_cols = [{"collection": "default", "document_count": 3, "chunk_count": 50}]
        with patch("quarry.http_server.db_list_collections", return_value=mock_cols):
            data = client.get("/collections").json()

        assert data["total_collections"] == 1
        assert data["collections"][0]["collection"] == "default"


class TestStatus:
    def test_returns_status(self, client: TestClient) -> None:
        with (
            patch("quarry.http_server.list_documents", return_value=[]),
            patch("quarry.http_server.count_chunks", return_value=0),
            patch("quarry.http_server.db_list_collections", return_value=[]),
        ):
            data = client.get("/status").json()

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
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        reg_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        fake_regs = [MagicMock(), MagicMock()]
        with (
            patch("quarry.http_server.list_documents", return_value=[]),
            patch("quarry.http_server.count_chunks", return_value=0),
            patch("quarry.http_server.db_list_collections", return_value=[]),
            patch("quarry.http_server.open_registry", return_value=MagicMock()),
            patch("quarry.http_server.list_registrations", return_value=fake_regs),
        ):
            data = reg_client.get("/status").json()

        assert "registered_directories" in data
        assert isinstance(data["registered_directories"], int)
        assert data["registered_directories"] == 2

    def test_registered_directories_zero_when_no_registry(self, tmp_path: Path) -> None:
        """When registry_path does not exist, registered_directories must be 0."""
        settings = _mock_settings(tmp_path)
        # registry_path points to a non-existent file
        settings.registry_path = tmp_path / "no-registry.db"
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        no_reg_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        with (
            patch("quarry.http_server.list_documents", return_value=[]),
            patch("quarry.http_server.count_chunks", return_value=0),
            patch("quarry.http_server.db_list_collections", return_value=[]),
        ):
            data = no_reg_client.get("/status").json()

        assert data["registered_directories"] == 0


class TestNotFound:
    def test_unknown_path_returns_404(self, client: TestClient) -> None:
        resp = client.get("/unknown")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Not Found"


class TestPortFile:
    def test_write_port_file(self, tmp_path: Path) -> None:
        port_path = tmp_path / "subdir" / "serve.port"
        _write_port_file(port_path, 12345)

        assert port_path.exists()
        assert port_path.read_text() == "12345"

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        port_path = tmp_path / "a" / "b" / "serve.port"
        _write_port_file(port_path, 8080)
        assert port_path.exists()


class TestFailClosed:
    """Non-loopback hosts require --api-key."""

    def test_non_loopback_without_key_refuses(self) -> None:
        with pytest.raises(SystemExit, match="Refusing to bind"):
            _validate_host_key("0.0.0.0", None)  # noqa: S104

    def test_loopback_without_key_allowed(self) -> None:
        _validate_host_key("127.0.0.1", None)


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


class TestCorsOrigins:
    """Test configurable CORS origin reflection."""

    @pytest.fixture()
    def cors_client(self, tmp_path: Path) -> TestClient:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(
            settings,
            cors_origins=frozenset(
                {
                    "https://punt-labs.com",
                    "http://localhost:4321",
                }
            ),
        )
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER

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
        """Default fixture has no cors_origins — falls back to http://localhost."""
        resp = client.get("/health", headers={"Origin": "http://localhost"})
        assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"


# --- API key auth tests ---

_TEST_API_KEY = "test-key-for-auth-testing"


@pytest.fixture()
def auth_client(tmp_path: Path) -> TestClient:
    """Build a test app with API key auth enabled."""
    settings = _mock_settings(tmp_path)
    ctx = _QuarryContext(settings, api_key=_TEST_API_KEY)
    ctx.__dict__["db"] = _SHARED_DB
    ctx.__dict__["embedder"] = _SHARED_EMBEDDER

    app = build_app(ctx)
    return TestClient(app, raise_server_exceptions=False)


class TestApiKeyAuth:
    """Test Bearer token authentication on the HTTP server."""

    def test_health_exempt_without_key(self, auth_client: TestClient) -> None:
        data = auth_client.get("/health").json()
        assert data["status"] == "ok"

    def test_search_rejected_without_key(self, auth_client: TestClient) -> None:
        assert auth_client.get("/search?q=test").status_code == 401

    def test_search_rejected_with_wrong_key(self, auth_client: TestClient) -> None:
        resp = auth_client.get(
            "/search?q=test", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401

    def test_search_allowed_with_correct_key(self, auth_client: TestClient) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]):
            data = auth_client.get(
                "/search?q=test",
                headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
            ).json()
        assert data["query"] == "test"

    def test_documents_rejected_without_key(self, auth_client: TestClient) -> None:
        assert auth_client.get("/documents").status_code == 401

    def test_documents_allowed_with_key(self, auth_client: TestClient) -> None:
        with patch("quarry.http_server.list_documents", return_value=[]):
            data = auth_client.get(
                "/documents",
                headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
            ).json()
        assert data["total_documents"] == 0

    def test_no_auth_required_when_key_not_configured(self, client: TestClient) -> None:
        """The default client fixture has no api_key — all open."""
        with patch("quarry.http_server.hybrid_search", return_value=[]):
            data = client.get("/search?q=test").json()
        assert data["query"] == "test"

    def test_malformed_auth_header_rejected(self, auth_client: TestClient) -> None:
        resp = auth_client.get(
            "/search?q=test", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert resp.status_code == 401

    def test_bearer_scheme_case_insensitive(self, auth_client: TestClient) -> None:
        """RFC 7235: auth scheme names are case-insensitive."""
        with patch("quarry.http_server.hybrid_search", return_value=[]):
            data = auth_client.get(
                "/search?q=test",
                headers={"Authorization": f"bearer {_TEST_API_KEY}"},
            ).json()
        assert data["query"] == "test"


class TestEmptyApiKey:
    """Empty API key string should not enable auth."""

    @pytest.fixture()
    def empty_key_client(self, tmp_path: Path) -> TestClient:
        settings = _mock_settings(tmp_path)
        ctx = _QuarryContext(settings, api_key="")
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER

        app = build_app(ctx)
        return TestClient(app, raise_server_exceptions=False)

    def test_empty_key_does_not_require_auth(
        self, empty_key_client: TestClient
    ) -> None:
        with patch("quarry.http_server.hybrid_search", return_value=[]):
            data = empty_key_client.get("/search?q=test").json()
        assert data["query"] == "test"
