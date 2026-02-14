"""Tests for the quarry HTTP server (quarry serve).

Uses a real HTTP server on an OS-assigned port with mocked database
and embedding backends. Each test class gets its own server instance.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPResponse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest

from quarry.http_server import QuarryHTTPServer, _QuarryContext, _write_port_file


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
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
def server_url(tmp_path: Path):
    """Start a test HTTP server and yield its base URL."""
    settings = _mock_settings(tmp_path)
    ctx = _QuarryContext(settings)

    # Patch cached_property values on the instance's __dict__ so they
    # bypass the descriptor protocol and return our mocks directly.
    ctx.__dict__["db"] = _SHARED_DB
    ctx.__dict__["embedder"] = _SHARED_EMBEDDER

    server = QuarryHTTPServer(("127.0.0.1", 0), ctx)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    server.server_close()


def _get(url: str) -> dict[str, Any]:
    """GET a URL and parse JSON response."""
    with urlopen(url, timeout=5) as resp:  # noqa: S310
        body: dict[str, Any] = json.loads(resp.read())
        return body


def _get_status(url: str) -> int:
    """GET a URL and return status code, even on error."""
    try:
        with urlopen(url, timeout=5) as resp:  # noqa: S310
            status: int = resp.status
            return status
    except HTTPError as exc:
        return exc.code


def _get_response(url: str, method: str = "GET") -> HTTPResponse:
    """Return the raw response (caller must close)."""
    req = Request(url, method=method)  # noqa: S310
    resp: HTTPResponse = urlopen(req, timeout=5)  # noqa: S310  # type: ignore[assignment]
    return resp


class TestHealth:
    def test_returns_ok(self, server_url: str):
        data = _get(f"{server_url}/health")
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0

    def test_cors_headers(self, server_url: str):
        with urlopen(f"{server_url}/health", timeout=5) as resp:  # noqa: S310
            assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost"


class TestSearch:
    def test_missing_query_returns_400(self, server_url: str):
        status = _get_status(f"{server_url}/search")
        assert status == 400

    def test_empty_query_returns_400(self, server_url: str):
        status = _get_status(f"{server_url}/search?q=")
        assert status == 400

    def test_search_returns_results(self, server_url: str):
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
        with patch("quarry.http_server.search", return_value=mock_results):
            data = _get(f"{server_url}/search?q=hello")

        assert data["query"] == "hello"
        assert data["total_results"] == 1
        assert data["results"][0]["document_name"] == "test.pdf"
        assert data["results"][0]["similarity"] == 0.9

    def test_search_with_limit(self, server_url: str):
        with patch("quarry.http_server.search", return_value=[]) as mock_search:
            _get(f"{server_url}/search?q=hello&limit=5")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 5

    def test_search_limit_capped_at_50(self, server_url: str):
        with patch("quarry.http_server.search", return_value=[]) as mock_search:
            _get(f"{server_url}/search?q=hello&limit=999")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 50

    def test_search_negative_limit_clamped_to_1(self, server_url: str):
        with patch("quarry.http_server.search", return_value=[]) as mock_search:
            _get(f"{server_url}/search?q=hello&limit=-5")

        _, kwargs = mock_search.call_args
        assert kwargs["limit"] == 1

    def test_search_with_collection_filter(self, server_url: str):
        with patch("quarry.http_server.search", return_value=[]) as mock_search:
            _get(f"{server_url}/search?q=hello&collection=research")

        _, kwargs = mock_search.call_args
        assert kwargs["collection_filter"] == "research"

    def test_search_empty_results(self, server_url: str):
        with patch("quarry.http_server.search", return_value=[]):
            data = _get(f"{server_url}/search?q=nonexistent")

        assert data["total_results"] == 0
        assert data["results"] == []


class TestDocuments:
    def test_list_documents(self, server_url: str):
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
            data = _get(f"{server_url}/documents")

        assert data["total_documents"] == 1
        assert data["documents"][0]["document_name"] == "report.pdf"

    def test_list_documents_with_collection_filter(self, server_url: str):
        with patch("quarry.http_server.list_documents", return_value=[]) as mock_list:
            _get(f"{server_url}/documents?collection=research")

        _, kwargs = mock_list.call_args
        assert kwargs["collection_filter"] == "research"


class TestCollections:
    def test_list_collections(self, server_url: str):
        mock_cols = [{"collection": "default", "document_count": 3, "chunk_count": 50}]
        with patch("quarry.http_server.db_list_collections", return_value=mock_cols):
            data = _get(f"{server_url}/collections")

        assert data["total_collections"] == 1
        assert data["collections"][0]["collection"] == "default"


class TestStatus:
    def test_returns_status(self, server_url: str):
        with (
            patch("quarry.http_server.list_documents", return_value=[]),
            patch("quarry.http_server.count_chunks", return_value=0),
            patch("quarry.http_server.db_list_collections", return_value=[]),
        ):
            data = _get(f"{server_url}/status")

        assert data["document_count"] == 0
        assert data["chunk_count"] == 0
        assert data["collection_count"] == 0
        assert "database_path" in data
        assert "embedding_model" in data


class TestNotFound:
    def test_unknown_path_returns_404(self, server_url: str):
        status = _get_status(f"{server_url}/unknown")
        assert status == 404


class TestPortFile:
    def test_write_port_file(self, tmp_path: Path):
        port_path = tmp_path / "subdir" / "serve.port"
        _write_port_file(port_path, 12345)

        assert port_path.exists()
        assert port_path.read_text() == "12345"

    def test_write_creates_parent_directories(self, tmp_path: Path):
        port_path = tmp_path / "a" / "b" / "serve.port"
        _write_port_file(port_path, 8080)
        assert port_path.exists()


class TestOptionsPreflightCors:
    def test_options_returns_204(self, server_url: str):
        resp = _get_response(f"{server_url}/health", method="OPTIONS")
        assert resp.status == 204
        resp.close()
