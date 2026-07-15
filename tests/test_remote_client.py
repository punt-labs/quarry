"""Unit tests for the remote HTTP client (quarry.remote_client)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from quarry.remote_client import RemoteClient, RemoteError

_WS_CONFIG: dict[str, object] = {"url": "ws://localhost:8420/mcp", "headers": {}}


def _mock_connection(status: int, body: bytes) -> MagicMock:
    """Return a mock HTTP connection whose response yields *status* and *body*."""
    conn = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    conn.getresponse.return_value = resp
    return conn


class TestRemoteError:
    """RemoteError carries an HTTP status and a human-readable message."""

    def test_status_and_message(self) -> None:
        exc = RemoteError(404, "not found")
        assert exc.status == 404
        assert str(exc) == "not found"

    def test_raisable_and_catchable(self) -> None:
        with pytest.raises(RemoteError, match="boom") as info:
            raise RemoteError(500, "boom")
        assert info.value.status == 500


class TestRequestTls:
    """HTTPS connections require a loadable pinned CA cert."""

    def test_empty_ca_cert_raises_system_exit(self) -> None:
        config: dict[str, object] = {
            "url": "wss://host:8420/mcp",
            "ca_cert": "",
            "headers": {},
        }
        with pytest.raises(SystemExit, match="CA cert"):
            RemoteClient(config).get("/health")

    def test_none_ca_cert_raises_system_exit(self) -> None:
        config: dict[str, object] = {
            "url": "wss://host:8420/mcp",
            "ca_cert": None,
            "headers": {},
        }
        with pytest.raises(SystemExit, match="CA cert"):
            RemoteClient(config).get("/health")

    def test_unreadable_ca_cert_raises_system_exit(self, tmp_path: Path) -> None:
        config: dict[str, object] = {
            "url": "wss://host:8420/mcp",
            "ca_cert": str(tmp_path / "nonexistent-ca.crt"),
            "headers": {},
        }
        with pytest.raises(SystemExit, match="CA certificate"):
            RemoteClient(config).get("/health")


class TestRequestWrapper:
    """The request wrapper builds the connection and parses the response."""

    def test_get_delegates_to_request(self) -> None:
        with patch("quarry.remote_client.RemoteClient.request") as mock_req:
            mock_req.return_value = {"ok": True}
            result = RemoteClient(_WS_CONFIG).get("/status")
        mock_req.assert_called_once_with("GET", "/status")
        assert result == {"ok": True}

    def test_post_sends_json_body(self) -> None:
        conn = _mock_connection(200, b'{"ok": true}')
        with patch("http.client.HTTPConnection", return_value=conn):
            result = RemoteClient(_WS_CONFIG).request(
                "POST", "/remember", body={"text": "hello"}
            )
        assert result == {"ok": True}
        call = conn.request.call_args
        assert call[0][0] == "POST"
        # The client version-prefixes every engine route on the wire.
        assert call[0][1] == "/v1/remember"
        assert json.loads(call[1]["body"]) == {"text": "hello"}
        assert call[1]["headers"]["Content-Type"] == "application/json"

    def test_delete_no_body_omits_content_type(self) -> None:
        conn = _mock_connection(200, b'{"deleted": 5}')
        with patch("http.client.HTTPConnection", return_value=conn):
            result = RemoteClient(_WS_CONFIG).request("DELETE", "/documents?name=foo")
        assert result == {"deleted": 5}
        assert "Content-Type" not in conn.request.call_args[1]["headers"]

    def test_empty_body_returns_empty_dict(self) -> None:
        conn = _mock_connection(200, b"")
        with patch("http.client.HTTPConnection", return_value=conn):
            assert RemoteClient(_WS_CONFIG).get("/status") == {}

    def test_timeout_reaches_connection(self) -> None:
        conn = _mock_connection(200, b"{}")
        with patch("http.client.HTTPConnection", return_value=conn) as mock_cls:
            RemoteClient(_WS_CONFIG).request("POST", "/sync", body={}, timeout=600.0)
        assert mock_cls.call_args[1]["timeout"] == 600.0


class TestRequestErrorMapping:
    """Anomalous responses and connection failures map to RemoteError."""

    def test_non_2xx_raises_remote_error(self) -> None:
        conn = _mock_connection(404, b'{"error": "Not found"}')
        with (
            patch("http.client.HTTPConnection", return_value=conn),
            pytest.raises(RemoteError, match="HTTP 404") as info,
        ):
            RemoteClient(_WS_CONFIG).request("DELETE", "/documents?name=foo")
        assert info.value.status == 404

    def test_json_array_raises_remote_error(self) -> None:
        conn = _mock_connection(200, b'["not", "a", "dict"]')
        with (
            patch("http.client.HTTPConnection", return_value=conn),
            pytest.raises(RemoteError, match="expected JSON object"),
        ):
            RemoteClient(_WS_CONFIG).get("/status")

    def test_json_scalar_raises_remote_error(self) -> None:
        conn = _mock_connection(200, b"42")
        with (
            patch("http.client.HTTPConnection", return_value=conn),
            pytest.raises(RemoteError, match="expected JSON object"),
        ):
            RemoteClient(_WS_CONFIG).get("/status")

    def test_non_json_body_raises_remote_error(self) -> None:
        conn = _mock_connection(200, b"<html>502 Bad Gateway</html>")
        with (
            patch("http.client.HTTPConnection", return_value=conn),
            pytest.raises(RemoteError, match="non-JSON response"),
        ):
            RemoteClient(_WS_CONFIG).get("/status")

    def test_connection_refused_raises_remote_error_status_zero(self) -> None:
        conn = MagicMock()
        conn.request.side_effect = ConnectionRefusedError(111, "Connection refused")
        with (
            patch("http.client.HTTPConnection", return_value=conn),
            pytest.raises(RemoteError) as info,
        ):
            RemoteClient(_WS_CONFIG).get("/search?q=test")
        assert info.value.status == 0
        assert "localhost" in str(info.value)
        assert "8420" in str(info.value)


class TestAwaitTask:
    """The poll helper resolves a 202 task to a terminal result."""

    def test_completed_returns_results(self) -> None:
        completed = {"status": "completed", "results": {"removed": 2}}
        with patch("quarry.remote_client.RemoteClient.request", return_value=completed):
            result = RemoteClient(_WS_CONFIG).await_task("t1")
        assert result == {"removed": 2}

    def test_failed_exits_one(self) -> None:
        failed = {"status": "failed", "error": "purge blew up"}
        with (
            patch("quarry.remote_client.RemoteClient.request", return_value=failed),
            pytest.raises(typer.Exit) as info,
        ):
            RemoteClient(_WS_CONFIG).await_task("t1")
        assert info.value.exit_code == 1

    def test_timeout_exits_one(self) -> None:
        running = {"status": "running"}
        with (
            patch("quarry.remote_client.RemoteClient.request", return_value=running),
            patch("quarry.remote_client._POLL_TIMEOUT_S", 0.0),
            pytest.raises(typer.Exit) as info,
        ):
            RemoteClient(_WS_CONFIG).await_task("t1")
        assert info.value.exit_code == 1

    def test_transient_blip_retries_then_completes(self) -> None:
        completed = {"status": "completed", "results": {"removed": 1}}
        with (
            patch(
                "quarry.remote_client.RemoteClient.request",
                side_effect=[RemoteError(0, "connection reset"), completed],
            ),
            patch("quarry.remote_client.time.sleep"),
        ):
            result = RemoteClient(_WS_CONFIG).await_task("t1")
        assert result == {"removed": 1}

    def test_poll_hard_error_exits_one(self) -> None:
        with (
            patch(
                "quarry.remote_client.RemoteClient.request",
                side_effect=RemoteError(500, "boom"),
            ),
            pytest.raises(typer.Exit) as info,
        ):
            RemoteClient(_WS_CONFIG).await_task("t1")
        assert info.value.exit_code == 1


class TestFind:
    """find returns a ``(json_results, text)`` payload for the CLI to emit."""

    def test_returns_json_and_text(self) -> None:
        resp = {
            "results": [
                {
                    "document_name": "doc.pdf",
                    "collection": "default",
                    "page_number": 3,
                    "chunk_index": 7,
                    "page_type": "body",
                    "source_format": ".pdf",
                    "agent_handle": "rmh",
                    "memory_type": "fact",
                    "summary": "a summary",
                    "similarity": 0.85,
                    "text": "full text here",
                }
            ]
        }
        with patch("quarry.remote_client.RemoteClient.get", return_value=resp):
            json_results, text = RemoteClient(_WS_CONFIG).find(
                "query", 10, "", "", "", "", "", ""
            )
        assert len(json_results) == 1
        assert set(json_results[0].keys()) == {
            "document_name",
            "collection",
            "page_number",
            "chunk_index",
            "page_type",
            "source_format",
            "agent_handle",
            "memory_type",
            "summary",
            "similarity",
            "text",
        }
        assert "doc.pdf" in text

    def test_optional_filters_encoded_in_path(self) -> None:
        with patch(
            "quarry.remote_client.RemoteClient.get", return_value={"results": []}
        ) as mock_get:
            RemoteClient(_WS_CONFIG).find("q", 5, "math", "", "code", "", "rmh", "")
        path = mock_get.call_args[0][0]
        assert "collection=math" in path
        assert "page_type=code" in path
        assert "agent_handle=rmh" in path
        assert "document=" not in path

    def test_remote_error_exits_one(self) -> None:
        with (
            patch(
                "quarry.remote_client.RemoteClient.get",
                side_effect=RemoteError(0, "cannot connect"),
            ),
            pytest.raises(typer.Exit) as info,
        ):
            RemoteClient(_WS_CONFIG).find("q", 10, "", "", "", "", "", "")
        assert info.value.exit_code == 1

    def test_empty_results_returns_empty_payload(self) -> None:
        with patch(
            "quarry.remote_client.RemoteClient.get", return_value={"results": []}
        ):
            json_results, text = RemoteClient(_WS_CONFIG).find(
                "q", 10, "", "", "", "", "", ""
            )
        assert json_results == []
        assert text == ""


class TestVersionPrefix:
    """Every engine route the client sends is /v1-prefixed on the wire.

    Locks the client half of the bug-class-3 contract: the daemon serves engine
    routes only under /v1, so a client that dropped the prefix would 404.
    """

    @pytest.mark.parametrize(
        ("method", "logical", "wire"),
        [
            ("GET", "/search?q=x", "/v1/search?q=x"),
            ("GET", "/status", "/v1/status"),
            ("GET", "/tasks/abc", "/v1/tasks/abc"),
            ("POST", "/remember", "/v1/remember"),
            ("DELETE", "/documents?name=foo", "/v1/documents?name=foo"),
        ],
    )
    def test_request_version_prefixes_path(
        self, method: str, logical: str, wire: str
    ) -> None:
        conn = _mock_connection(200, b"{}")
        with patch("http.client.HTTPConnection", return_value=conn):
            RemoteClient(_WS_CONFIG).request(method, logical)
        assert conn.request.call_args[0][1] == wire
