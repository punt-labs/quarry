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


class TestShow:
    """Tests for GET /show endpoint."""

    def test_show_page_text(self, client: TestClient) -> None:
        with patch("quarry.http_server.get_page_text", return_value="Hello world"):
            data = client.get("/show?document=foo&page=1").json()

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
        with patch("quarry.http_server.list_documents", return_value=[mock_doc]):
            data = client.get("/show?document=foo").json()

        assert data["document_name"] == "foo"
        assert data["total_pages"] == 10

    def test_show_missing_document_param(self, client: TestClient) -> None:
        resp = client.get("/show")
        assert resp.status_code == 400
        assert "document" in resp.json()["error"].lower()

    def test_show_empty_document_param(self, client: TestClient) -> None:
        resp = client.get("/show?document=")
        assert resp.status_code == 400

    def test_show_page_not_found(self, client: TestClient) -> None:
        with patch("quarry.http_server.get_page_text", return_value=None):
            resp = client.get("/show?document=foo&page=1")

        assert resp.status_code == 404
        assert resp.json()["error"] == "Not found"

    def test_show_document_not_found(self, client: TestClient) -> None:
        with patch("quarry.http_server.list_documents", return_value=[]):
            resp = client.get("/show?document=missing")

        assert resp.status_code == 404
        assert resp.json()["error"] == "Not found"

    def test_show_with_collection(self, client: TestClient) -> None:
        with patch(
            "quarry.http_server.get_page_text", return_value="page text"
        ) as mock_get_page:
            client.get("/show?document=foo&page=1&collection=math")

        _, kwargs = mock_get_page.call_args
        assert kwargs["collection"] == "math"

    def test_show_metadata_with_collection(self, client: TestClient) -> None:
        with patch("quarry.http_server.list_documents", return_value=[]) as mock_list:
            client.get("/show?document=foo&collection=math")

        _, kwargs = mock_list.call_args
        assert kwargs["collection_filter"] == "math"

    def test_show_invalid_page_returns_400(self, client: TestClient) -> None:
        resp = client.get("/show?document=foo&page=abc")
        assert resp.status_code == 400
        assert "Invalid page number" in resp.json()["error"]

    def test_show_negative_page_returns_400(self, client: TestClient) -> None:
        resp = client.get("/show?document=foo&page=-1")
        assert resp.status_code == 400
        assert "must be >= 1" in resp.json()["error"]

    def test_show_zero_page_returns_400(self, client: TestClient) -> None:
        resp = client.get("/show?document=foo&page=0")
        assert resp.status_code == 400
        assert "must be >= 1" in resp.json()["error"]


class TestDeleteDocuments:
    """Tests for DELETE /documents endpoint."""

    def test_delete_document(self, client: TestClient) -> None:
        with patch("quarry.http_server.db_delete_document", return_value=15):
            data = client.delete("/documents?name=foo").json()

        assert data["deleted"] == 15
        assert data["name"] == "foo"
        assert data["type"] == "document"

    def test_delete_document_not_found(self, client: TestClient) -> None:
        with patch("quarry.http_server.db_delete_document", return_value=0):
            resp = client.delete("/documents?name=foo")

        assert resp.status_code == 404
        assert resp.json()["error"] == "Not found"

    def test_delete_document_missing_name(self, client: TestClient) -> None:
        resp = client.delete("/documents")
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_delete_document_with_collection(self, client: TestClient) -> None:
        with patch("quarry.http_server.db_delete_document", return_value=5) as mock_del:
            client.delete("/documents?name=foo&collection=math")

        _, kwargs = mock_del.call_args
        assert kwargs["collection"] == "math"


class TestDeleteCollections:
    """Tests for DELETE /collections endpoint."""

    def test_delete_collection(self, client: TestClient) -> None:
        with patch("quarry.http_server.db_delete_collection", return_value=50):
            data = client.delete("/collections?name=math").json()

        assert data["deleted"] == 50
        assert data["name"] == "math"
        assert data["type"] == "collection"

    def test_delete_collection_not_found(self, client: TestClient) -> None:
        with patch("quarry.http_server.db_delete_collection", return_value=0):
            resp = client.delete("/collections?name=missing")

        assert resp.status_code == 404
        assert resp.json()["error"] == "Not found"

    def test_delete_collection_missing_name(self, client: TestClient) -> None:
        resp = client.delete("/collections")
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()


class TestRemember:
    """Tests for POST /remember endpoint."""

    def test_success(self, client: TestClient) -> None:
        mock_result = {
            "document_name": "notes.md",
            "collection": "default",
            "chunks": 3,
        }
        with patch(
            "quarry.pipeline.ingest_content", return_value=mock_result
        ) as mock_ingest:
            resp = client.post(
                "/remember",
                json={"name": "notes.md", "content": "hello world"},
            )

        assert resp.status_code == 200
        assert resp.json() == mock_result
        mock_ingest.assert_called_once()

    def test_missing_content_returns_400(self, client: TestClient) -> None:
        resp = client.post("/remember", json={"name": "notes.md"})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_missing_name_returns_400(self, client: TestClient) -> None:
        resp = client.post("/remember", json={"content": "hello"})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_empty_content_returns_400(self, client: TestClient) -> None:
        resp = client.post("/remember", json={"name": "a.md", "content": ""})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_whitespace_content_returns_400(self, client: TestClient) -> None:
        """Whitespace-only content must be rejected (matches local CLI)."""
        resp = client.post("/remember", json={"name": "a.md", "content": "   \n\n\t"})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"].lower()

    def test_whitespace_name_returns_400(self, client: TestClient) -> None:
        resp = client.post("/remember", json={"name": "   ", "content": "hello"})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/remember",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_pipeline_value_error_returns_400(self, client: TestClient) -> None:
        """ingest_content raising ValueError maps to HTTP 400, not 500."""
        with patch(
            "quarry.pipeline.ingest_content",
            side_effect=ValueError("bad content encoding"),
        ):
            resp = client.post(
                "/remember",
                json={"name": "n.md", "content": "body"},
            )
        assert resp.status_code == 400
        assert "bad content encoding" in resp.json()["error"]

    def test_pipeline_os_error_returns_502(self, client: TestClient) -> None:
        """ingest_content raising OSError maps to HTTP 502, not 500."""
        with patch(
            "quarry.pipeline.ingest_content",
            side_effect=OSError("disk full"),
        ):
            resp = client.post(
                "/remember",
                json={"name": "n.md", "content": "body"},
            )
        assert resp.status_code == 502
        assert "disk full" in resp.json()["error"]

    def test_rejects_oversized_body(self, client: TestClient) -> None:
        """Remember body > 50 MB must be rejected with HTTP 413."""
        from quarry.http_server import MAX_REMEMBER_BODY_BYTES

        too_big = MAX_REMEMBER_BODY_BYTES + 1
        resp = client.post(
            "/remember",
            content=b"x",  # actual body tiny; Content-Length header lies
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(too_big),
            },
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["error"].lower()

    def test_passes_all_params(self, client: TestClient) -> None:
        with patch(
            "quarry.pipeline.ingest_content",
            return_value={"document_name": "n", "collection": "c", "chunks": 1},
        ) as mock_ingest:
            client.post(
                "/remember",
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

    def test_overwrite_defaults_true(self, client: TestClient) -> None:
        with patch(
            "quarry.pipeline.ingest_content",
            return_value={"document_name": "n", "collection": "c", "chunks": 1},
        ) as mock_ingest:
            client.post(
                "/remember",
                json={"name": "n.md", "content": "body"},
            )

        assert mock_ingest.call_args.kwargs["overwrite"] is True

    def test_rejects_non_bool_overwrite(self, client: TestClient) -> None:
        """Strings like 'false' or '0' must not be silently coerced to True."""
        resp = client.post(
            "/remember",
            json={"name": "n.md", "content": "body", "overwrite": "false"},
        )
        assert resp.status_code == 400
        assert "overwrite" in resp.json()["error"].lower()
        assert "boolean" in resp.json()["error"].lower()

    def test_rejects_integer_overwrite(self, client: TestClient) -> None:
        resp = client.post(
            "/remember",
            json={"name": "n.md", "content": "body", "overwrite": 0},
        )
        assert resp.status_code == 400
        assert "overwrite" in resp.json()["error"].lower()


def _fake_public_addrinfo(
    _host: str,
    *_args: object,
    **_kwargs: object,
) -> list[tuple[object, object, object, str, tuple[str, int]]]:
    """Stand in for socket.getaddrinfo() — resolves every host to 93.184.216.34."""
    return [(None, None, None, "", ("93.184.216.34", 0))]


class TestIngest:
    """Tests for POST /ingest endpoint."""

    def test_success_with_url(self, client: TestClient) -> None:
        mock_result = {
            "document_name": "https://example.com",
            "collection": "example.com",
            "chunks": 5,
        }
        with (
            patch(
                "quarry.http_server.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.pipeline.ingest_auto", return_value=mock_result
            ) as mock_ingest,
        ):
            resp = client.post("/ingest", json={"source": "https://example.com/docs"})

        assert resp.status_code == 200
        assert resp.json() == mock_result
        mock_ingest.assert_called_once()

    def test_missing_source_returns_400(self, client: TestClient) -> None:
        resp = client.post("/ingest", json={})
        assert resp.status_code == 400
        assert "source" in resp.json()["error"].lower()

    def test_non_url_source_returns_400(self, client: TestClient) -> None:
        resp = client.post("/ingest", json={"source": "/path/to/file.pdf"})
        assert resp.status_code == 400
        assert "url" in resp.json()["error"].lower()

    def test_empty_source_returns_400(self, client: TestClient) -> None:
        resp = client.post("/ingest", json={"source": ""})
        assert resp.status_code == 400

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/ingest",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_passes_all_params(self, client: TestClient) -> None:
        with (
            patch(
                "quarry.http_server.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.pipeline.ingest_auto",
                return_value={"document_name": "d", "collection": "c", "chunks": 1},
            ) as mock_ingest,
        ):
            client.post(
                "/ingest",
                json={
                    "source": "https://example.com/docs",
                    "overwrite": True,
                    "collection": "mycol",
                    "agent_handle": "rmh",
                    "memory_type": "fact",
                    "summary": "one line",
                },
            )

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
            "quarry.http_server.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/ingest", json={"source": "http://192.168.1.1/"})
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
            "quarry.http_server.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/ingest", json={"source": "http://127.0.0.1/"})
        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()

    def test_rejects_metadata_ip(self, client: TestClient) -> None:
        """Cloud metadata endpoint must be blocked without even resolving."""
        with patch(
            "quarry.http_server.socket_module.getaddrinfo",
        ) as mock_resolve:
            resp = client.post(
                "/ingest",
                json={"source": "http://169.254.169.254/latest/meta-data/"},
            )
        assert resp.status_code == 400
        assert "metadata" in resp.json()["error"].lower()
        mock_resolve.assert_not_called()

    def test_rejects_dotlocal(self, client: TestClient) -> None:
        """mDNS .local hostnames must be blocked pre-resolution."""
        with patch(
            "quarry.http_server.socket_module.getaddrinfo",
        ) as mock_resolve:
            resp = client.post("/ingest", json={"source": "http://myserver.local/"})
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
            "quarry.http_server.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post(
                "/ingest", json={"source": "http://autoconfigured.example/"}
            )
        assert resp.status_code == 400
        assert "rejected" in resp.json()["error"].lower()

    def test_pipeline_value_error_returns_400(self, client: TestClient) -> None:
        """ingest_auto raising ValueError maps to HTTP 400."""
        with (
            patch(
                "quarry.http_server.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.pipeline.ingest_auto",
                side_effect=ValueError("unsupported URL"),
            ),
        ):
            resp = client.post("/ingest", json={"source": "https://example.com/"})
        assert resp.status_code == 400
        assert "unsupported URL" in resp.json()["error"]

    def test_pipeline_os_error_returns_502(self, client: TestClient) -> None:
        """ingest_auto raising OSError maps to HTTP 502 (bad gateway)."""
        with (
            patch(
                "quarry.http_server.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.pipeline.ingest_auto",
                side_effect=OSError("upstream refused connection"),
            ),
        ):
            resp = client.post("/ingest", json={"source": "https://example.com/"})
        assert resp.status_code == 502
        assert "upstream refused connection" in resp.json()["error"]

    def test_rejects_oversized_body(self, client: TestClient) -> None:
        """Ingest body > 1 MB must be rejected with HTTP 413."""
        from quarry.http_server import MAX_INGEST_BODY_BYTES

        too_big = MAX_INGEST_BODY_BYTES + 1
        resp = client.post(
            "/ingest",
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
            "/ingest",
            json={"source": "https://example.com/", "overwrite": "false"},
        )
        assert resp.status_code == 400
        assert "overwrite" in resp.json()["error"].lower()
        assert "boolean" in resp.json()["error"].lower()

    def test_accepts_uppercase_scheme(self, client: TestClient) -> None:
        """HTTPS:// (uppercase) must be accepted — scheme is case-insensitive."""
        mock_result = {
            "document_name": "https://example.com",
            "collection": "example.com",
            "chunks": 1,
        }
        with (
            patch(
                "quarry.http_server.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.pipeline.ingest_auto", return_value=mock_result
            ) as mock_ingest,
        ):
            resp = client.post("/ingest", json={"source": "HTTPS://example.com/docs"})

        assert resp.status_code == 200
        mock_ingest.assert_called_once()

    def test_rejects_cgnat(self, client: TestClient) -> None:
        """RFC 6598 CGNAT addresses (100.64.0.0/10) must be blocked."""

        def fake_getaddrinfo(
            _host: str,
            *_a: object,
            **_kw: object,
        ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
            return [(None, None, None, "", ("100.64.1.1", 0))]

        with patch(
            "quarry.http_server.socket_module.getaddrinfo",
            side_effect=fake_getaddrinfo,
        ):
            resp = client.post("/ingest", json={"source": "http://cgnat.example/"})
        assert resp.status_code == 400
        assert "cgnat" in resp.json()["error"].lower()


class TestSync:
    """Tests for POST /sync endpoint."""

    def _sync_result(
        self,
        collection: str = "math",
        *,
        ingested: int = 3,
        deleted: int = 1,
        skipped: int = 5,
        failed: int = 0,
        errors: list[str] | None = None,
    ) -> object:
        from quarry.sync import SyncResult

        return SyncResult(
            collection=collection,
            ingested=ingested,
            deleted=deleted,
            skipped=skipped,
            failed=failed,
            errors=errors or [],
        )

    def test_success(self, client: TestClient) -> None:
        mock_results = {"math": self._sync_result()}
        with patch("quarry.sync.sync_all", return_value=mock_results):
            resp = client.post("/sync", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "math": {
                "ingested": 3,
                "deleted": 1,
                "skipped": 5,
                "failed": 0,
                "errors": [],
            }
        }

    def test_empty_body_accepted(self, client: TestClient) -> None:
        with patch("quarry.sync.sync_all", return_value={}):
            resp = client.post(
                "/sync",
                content=b"",
                headers={"Content-Length": "0"},
            )
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/sync",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_non_object_body_returns_400(self, client: TestClient) -> None:
        resp = client.post("/sync", json=[1, 2, 3])
        assert resp.status_code == 400

    def test_auth_required(self, auth_client: TestClient) -> None:
        resp = auth_client.post("/sync", json={})
        assert resp.status_code == 401

    def test_auth_allows_with_key(self, auth_client: TestClient) -> None:
        with patch("quarry.sync.sync_all", return_value={}):
            resp = auth_client.post(
                "/sync",
                json={},
                headers={"Authorization": f"Bearer {_TEST_API_KEY}"},
            )
        assert resp.status_code == 200

    def test_pipeline_value_error_returns_400(self, client: TestClient) -> None:
        with patch("quarry.sync.sync_all", side_effect=ValueError("bad reg")):
            resp = client.post("/sync", json={})
        assert resp.status_code == 400
        assert "bad reg" in resp.json()["error"]

    def test_pipeline_os_error_returns_502(self, client: TestClient) -> None:
        with patch("quarry.sync.sync_all", side_effect=OSError("disk full")):
            resp = client.post("/sync", json={})
        assert resp.status_code == 502
        assert "disk full" in resp.json()["error"]

    def test_rejects_oversized_body(self, client: TestClient) -> None:
        from quarry.http_server import MAX_SYNC_BODY_BYTES

        too_big = MAX_SYNC_BODY_BYTES + 1
        resp = client.post(
            "/sync",
            content=b"x",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(too_big),
            },
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["error"].lower()

    def test_errors_list_passed_through(self, client: TestClient) -> None:
        mock_results = {
            "col": self._sync_result(
                "col",
                ingested=1,
                failed=2,
                errors=["a.pdf: corrupt", "b.pdf: timeout"],
            )
        }
        with patch("quarry.sync.sync_all", return_value=mock_results):
            resp = client.post("/sync", json={})
        assert resp.status_code == 200
        assert resp.json()["col"]["errors"] == ["a.pdf: corrupt", "b.pdf: timeout"]


class TestDatabases:
    """Tests for GET /databases endpoint."""

    def test_returns_single_entry_list(self, client: TestClient) -> None:
        with patch("quarry.http_server.list_documents", return_value=[{"x": 1}]):
            resp = client.get("/databases")

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
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        app = build_app(ctx)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("quarry.http_server.list_documents", return_value=[]):
            data = client.get("/databases").json()

        assert data["databases"][0]["name"] == "work"

    def test_auth_required(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/databases")
        assert resp.status_code == 401


class TestUse:
    """Tests for POST /use endpoint."""

    def test_returns_400_not_supported(self, client: TestClient) -> None:
        resp = client.post("/use", json={"name": "work"})
        assert resp.status_code == 400
        error = resp.json()["error"].lower()
        assert "client-side" in error

    def test_auth_required(self, auth_client: TestClient) -> None:
        resp = auth_client.post("/use", json={"name": "work"})
        assert resp.status_code == 401


class TestRegistrations:
    """Tests for GET/POST/DELETE /registrations endpoint."""

    @pytest.fixture()
    def home_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        """A client whose settings live under ``tmp_path``.

        Also patches ``_server_home`` to return ``tmp_path`` so that
        registrations under ``tmp_path`` pass the path allowlist.  The
        real, pwd-backed resolver returns the runner's ``$HOME`` in CI,
        which is outside ``tmp_path`` — on dev laptops pytest puts
        ``tmp_path`` inside ``$HOME`` by coincidence, hiding the failure
        until CI runs.
        """
        home = tmp_path / "home"
        home.mkdir()
        settings = _mock_settings(tmp_path)
        settings.registry_path = home / "registry.db"
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        app = build_app(ctx)
        resolved = tmp_path.resolve()
        monkeypatch.setattr(
            "quarry.http_server._server_home",
            lambda: (resolved, None),
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_get_empty_when_no_registry(self, client: TestClient) -> None:
        resp = client.get("/registrations")
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
            patch(
                "quarry.http_server.open_registry",
                return_value=MagicMock(close=MagicMock()),
            ),
            patch("quarry.http_server.list_registrations", return_value=regs),
            patch(
                "pathlib.Path.exists",
                return_value=True,
            ),
        ):
            data = client.get("/registrations").json()

        assert data["total_registrations"] == 1
        entry = data["registrations"][0]
        assert entry["collection"] == "math"
        assert entry["directory"] == "/home/u/math"
        assert entry["registered_at"] == "2026-01-01T00:00:00"

    def test_post_registers_directory(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        target = home / "docs"
        target.mkdir()

        resp = home_client.post(
            "/registrations",
            json={"directory": str(target), "collection": "docs"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["collection"] == "docs"
        assert data["directory"] == str(target.resolve())
        assert "registered_at" in data

    def test_post_rejects_missing_directory(self, client: TestClient) -> None:
        resp = client.post("/registrations", json={"collection": "c"})
        assert resp.status_code == 400
        assert "directory" in resp.json()["error"].lower()

    def test_post_rejects_missing_collection(self, client: TestClient) -> None:
        resp = client.post("/registrations", json={"directory": "/home/x"})
        assert resp.status_code == 400
        assert "collection" in resp.json()["error"].lower()

    def test_post_rejects_path_with_parent_segment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        resp = client.post(
            "/registrations",
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
            "/registrations",
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
            "/registrations",
            json={"directory": str(missing), "collection": "c"},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"].lower()

    def test_post_conflict_returns_409(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        target = home / "docs"
        target.mkdir()

        # Register once, then try again.
        resp1 = home_client.post(
            "/registrations",
            json={"directory": str(target), "collection": "docs"},
        )
        assert resp1.status_code == 200
        resp2 = home_client.post(
            "/registrations",
            json={"directory": str(target), "collection": "docs"},
        )
        assert resp2.status_code == 409

    def test_delete_success(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        target = home / "docs"
        target.mkdir()
        home_client.post(
            "/registrations",
            json={"directory": str(target), "collection": "docs"},
        )

        with patch("quarry.http_server.db_delete_document", return_value=0):
            resp = home_client.delete("/registrations?collection=docs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["collection"] == "docs"
        assert data["type"] == "registration"

    def test_delete_also_removes_documents(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DELETE without keep_data must call db_delete_document per file.

        Regression for the class-3 divergence bug where the remote path
        left LanceDB chunks indexed after a deregister.
        """
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        target = home / "docs"
        target.mkdir()

        home_client.post(
            "/registrations",
            json={"directory": str(target), "collection": "docs"},
        )

        fake_docs = ["a.pdf", "sub/b.txt", "c.md"]
        with (
            patch(
                "quarry.http_server.deregister_directory",
                return_value=fake_docs,
            ),
            patch(
                "quarry.http_server.db_delete_document",
                return_value=5,
            ) as mock_del,
        ):
            resp = home_client.delete("/registrations?collection=docs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == 3
        assert data["deleted_chunks"] == 15  # 3 docs * 5 chunks
        assert mock_del.call_count == 3
        called_names = [call.args[1] for call in mock_del.call_args_list]
        assert called_names == fake_docs
        for call in mock_del.call_args_list:
            assert call.kwargs["collection"] == "docs"

    def test_delete_keep_data(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DELETE with keep_data=true must not call db_delete_document."""
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        target = home / "docs"
        target.mkdir()

        home_client.post(
            "/registrations",
            json={"directory": str(target), "collection": "docs"},
        )

        with (
            patch(
                "quarry.http_server.deregister_directory",
                return_value=["a.pdf"],
            ),
            patch("quarry.http_server.db_delete_document") as mock_del,
        ):
            resp = home_client.delete("/registrations?collection=docs&keep_data=true")

        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == 1
        assert data["deleted_chunks"] == 0
        mock_del.assert_not_called()

    def test_delete_rejects_invalid_keep_data(
        self,
        home_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        resp = home_client.delete("/registrations?collection=docs&keep_data=yes")
        assert resp.status_code == 400
        assert "keep_data" in resp.json()["error"]

    def test_delete_missing_collection_param(self, client: TestClient) -> None:
        resp = client.delete("/registrations")
        assert resp.status_code == 400

    def test_delete_not_found(self, home_client: TestClient) -> None:
        resp = home_client.delete("/registrations?collection=missing")
        assert resp.status_code == 404

    def test_delete_no_registry_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/registrations?collection=anything")
        assert resp.status_code == 404

    def test_auth_required_get(self, auth_client: TestClient) -> None:
        assert auth_client.get("/registrations").status_code == 401

    def test_auth_required_post(self, auth_client: TestClient) -> None:
        assert (
            auth_client.post(
                "/registrations",
                json={"directory": "/home/x", "collection": "c"},
            ).status_code
            == 401
        )

    def test_auth_required_delete(self, auth_client: TestClient) -> None:
        assert auth_client.delete("/registrations?collection=c").status_code == 401

    def test_post_rejects_oversized_body(self, client: TestClient) -> None:
        from quarry.http_server import MAX_REGISTRATIONS_BODY_BYTES

        too_big = MAX_REGISTRATIONS_BODY_BYTES + 1
        resp = client.post(
            "/registrations",
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
        from quarry.http_server import _check_body_size

        request = MagicMock()
        request.headers = {"content-length": "100"}
        assert _check_body_size(request, 200) is None

    def test_rejects_oversized(self) -> None:
        from quarry.http_server import _check_body_size

        request = MagicMock()
        request.headers = {"content-length": "300"}
        resp = _check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 413

    def test_rejects_missing_content_length(self) -> None:
        from quarry.http_server import _check_body_size

        request = MagicMock()
        request.headers = {}
        resp = _check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 411

    def test_rejects_non_numeric_content_length(self) -> None:
        from quarry.http_server import _check_body_size

        request = MagicMock()
        request.headers = {"content-length": "not-a-number"}
        resp = _check_body_size(request, 200)
        assert resp is not None
        assert resp.status_code == 400

    def test_rejects_negative_content_length(self) -> None:
        from quarry.http_server import _check_body_size

        request = MagicMock()
        request.headers = {"content-length": "-10"}
        resp = _check_body_size(request, 200)
        assert resp is not None
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
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        test_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        # Even if an attacker could unset HOME, the server consults pwd.
        monkeypatch.delenv("HOME", raising=False)

        def _boom(_uid: int) -> object:
            raise KeyError("uid not in passwd")

        with patch("quarry.http_server.pwd.getpwuid", side_effect=_boom):
            resp = test_client.post(
                "/registrations",
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
        ctx = _QuarryContext(settings)
        ctx.__dict__["db"] = _SHARED_DB
        ctx.__dict__["embedder"] = _SHARED_EMBEDDER
        test_client = TestClient(build_app(ctx), raise_server_exceptions=False)

        monkeypatch.setenv("HOME", str(fake_home))
        real_entry = MagicMock()
        real_entry.pw_dir = str(real_home)

        # Registering somewhere under real_home must succeed even though
        # the env says HOME=fakehome.
        target = real_home / "docs"
        target.mkdir()
        with patch("quarry.http_server.pwd.getpwuid", return_value=real_entry):
            resp = test_client.post(
                "/registrations",
                json={"directory": str(target), "collection": "docs"},
            )
        assert resp.status_code == 200

        # And registering outside pw_dir fails, even if HOME covers it.
        outside = fake_home / "docs"
        outside.mkdir()
        with patch("quarry.http_server.pwd.getpwuid", return_value=real_entry):
            resp = test_client.post(
                "/registrations",
                json={"directory": str(outside), "collection": "evil"},
            )
        assert resp.status_code == 400
        assert "outside" in resp.json()["error"].lower()


class TestDatabasesMissingTable:
    """Fresh databases (no chunks table) must still respond to /databases."""

    def test_list_documents_failure_is_zero(self, client: TestClient) -> None:
        """A raise from list_documents must degrade to document_count=0."""
        with patch(
            "quarry.http_server.list_documents",
            side_effect=ValueError("Table 'chunks' was not found"),
        ):
            resp = client.get("/databases")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_databases"] == 1
        assert data["databases"][0]["document_count"] == 0


class TestSyncGenericFailure:
    """Fix 8: ``_sync_route`` wraps generic exceptions in a JSON envelope."""

    def test_runtime_error_returns_500_json(self, client: TestClient) -> None:
        with patch(
            "quarry.sync.sync_all",
            side_effect=RuntimeError("embedder crashed"),
        ):
            resp = client.post("/sync", json={})

        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert "embedder crashed" in body["error"]
        assert "sync failed" in body["error"]

    def test_remember_runtime_error_returns_500_json(self, client: TestClient) -> None:
        with patch(
            "quarry.pipeline.ingest_content",
            side_effect=RuntimeError("embedder crashed"),
        ):
            resp = client.post(
                "/remember",
                json={"name": "n.md", "content": "body"},
            )
        assert resp.status_code == 500
        body = resp.json()
        assert "embedder crashed" in body["error"]
        assert "remember failed" in body["error"]

    def test_ingest_runtime_error_returns_500_json(self, client: TestClient) -> None:
        with (
            patch(
                "quarry.http_server.socket_module.getaddrinfo",
                side_effect=_fake_public_addrinfo,
            ),
            patch(
                "quarry.pipeline.ingest_auto",
                side_effect=RuntimeError("embedder crashed"),
            ),
        ):
            resp = client.post(
                "/ingest",
                json={"source": "https://example.com/"},
            )
        assert resp.status_code == 500
        body = resp.json()
        assert "embedder crashed" in body["error"]
        assert "ingest failed" in body["error"]
