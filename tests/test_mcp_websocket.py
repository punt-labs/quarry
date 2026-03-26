"""Tests for the MCP-over-WebSocket endpoint (/mcp).

Uses Starlette's TestClient WebSocket support to exercise the full path:
client → WebSocket → mcp.server.websocket → FastMCP tool dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quarry.http_server import _QuarryContext, build_app


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    s.quarry_root = tmp_path / ".punt-labs" / "quarry"
    s.registry_path = tmp_path / ".punt-labs" / "quarry" / "registry.db"
    return s


def _mock_embedder() -> MagicMock:
    embedder = MagicMock()
    embedder.embed_query.return_value = np.zeros(768, dtype=np.float32)
    return embedder


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = _mock_settings(tmp_path)
    ctx = _QuarryContext(settings)
    ctx.__dict__["db"] = MagicMock()
    ctx.__dict__["embedder"] = _mock_embedder()

    app = build_app(ctx)
    return TestClient(app)


@pytest.fixture()
def auth_client(tmp_path: Path) -> TestClient:
    settings = _mock_settings(tmp_path)
    ctx = _QuarryContext(settings, api_key="test-ws-key")
    ctx.__dict__["db"] = MagicMock()
    ctx.__dict__["embedder"] = _mock_embedder()

    app = build_app(ctx)
    return TestClient(app)


def _jsonrpc_request(
    method: str, id: int = 1, params: dict[str, object] | None = None
) -> str:
    msg: dict[str, object] = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


class TestMcpWebSocket:
    def test_websocket_connects(self, client: TestClient) -> None:
        """Basic WebSocket upgrade succeeds."""
        with client.websocket_connect("/mcp?session_key=test-pid") as ws:
            # Send MCP initialize request.
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            resp = json.loads(ws.receive_text())
            assert resp["jsonrpc"] == "2.0"
            assert resp["id"] == 1
            assert "result" in resp
            assert "serverInfo" in resp["result"]

    def test_websocket_session_key_logged(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Session key appears in connection log."""
        import logging

        with (
            caplog.at_level(logging.INFO),
            client.websocket_connect("/mcp?session_key=abc123") as ws,
        ):
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            ws.receive_text()

        assert any("abc123" in r.getMessage() for r in caplog.records)

    def test_websocket_auth_required(self, auth_client: TestClient) -> None:
        """WebSocket without auth is rejected when api_key is configured."""
        with (
            pytest.raises(WebSocketDisconnect),
            auth_client.websocket_connect("/mcp") as ws,
        ):
            ws.receive_text()

    def test_websocket_auth_wrong_key(self, auth_client: TestClient) -> None:
        """WebSocket with wrong Bearer token is rejected."""
        with (
            pytest.raises(WebSocketDisconnect),
            auth_client.websocket_connect(
                "/mcp", headers={"Authorization": "Bearer wrong-key"}
            ) as ws,
        ):
            ws.receive_text()

    def test_websocket_auth_correct_key(self, auth_client: TestClient) -> None:
        """WebSocket with correct Bearer token succeeds."""
        with auth_client.websocket_connect(
            "/mcp", headers={"Authorization": "Bearer test-ws-key"}
        ) as ws:
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            resp = json.loads(ws.receive_text())
            assert "result" in resp

    def test_websocket_no_auth_when_key_not_configured(
        self, client: TestClient
    ) -> None:
        """Without api_key configured, WebSocket connects without auth."""
        with client.websocket_connect("/mcp") as ws:
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            resp = json.loads(ws.receive_text())
            assert "result" in resp

    def test_websocket_rejects_foreign_origin(self, client: TestClient) -> None:
        """Browser cross-origin WebSocket hijacking is blocked."""
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/mcp", headers={"Origin": "https://evil.com"}
            ) as ws,
        ):
            ws.receive_text()

    def test_websocket_allows_matching_origin(self, client: TestClient) -> None:
        """Allowed CORS origin can open a WebSocket."""
        with client.websocket_connect(
            "/mcp", headers={"Origin": "http://localhost"}
        ) as ws:
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            resp = json.loads(ws.receive_text())
            assert "result" in resp

    def test_websocket_allows_no_origin(self, client: TestClient) -> None:
        """Non-browser clients (no Origin header) are allowed."""
        with client.websocket_connect("/mcp") as ws:
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            resp = json.loads(ws.receive_text())
            assert "result" in resp

    def test_session_key_control_chars_sanitized(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Control characters in session_key are stripped from logs."""
        import logging

        with (
            caplog.at_level(logging.INFO),
            client.websocket_connect("/mcp?session_key=abc%0d%0aINJECTED") as ws,
        ):
            ws.send_text(
                _jsonrpc_request(
                    "initialize",
                    params={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                )
            )
            ws.receive_text()

        key_logs = [
            r.getMessage() for r in caplog.records if "session_key" in r.getMessage()
        ]
        assert key_logs
        for msg in key_logs:
            assert "\r" not in msg
            assert "\n" not in msg
            assert "INJECTED" in msg
