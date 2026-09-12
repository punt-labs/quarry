"""Wire-shape tests for individual :class:`QuarryClient` methods.

Complements ``test_client_errors.py`` (status classification): this file
asserts what a given client method actually PUTS on the wire — method, path,
and body — using the same ``httpx.MockTransport`` idiom.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from quarry.api import CapturesLookupResponse
from quarry.client.client import QuarryClient
from quarry.client.transport import HttpxTransport

if TYPE_CHECKING:
    import pytest

_Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: _Handler) -> QuarryClient:
    http_client = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    return QuarryClient(HttpxTransport(http_client))


class TestCapturesLookupWire:
    """POST, not a ``?url=`` query string — a secret token in *url* must
    never ride a query string that proxy/WAF/browser logs can capture
    (CWE-598)."""

    def test_sends_post_with_json_body_not_query_params(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"matched": False, "document_name": None})

        result = _client(handler).captures_lookup(
            "https://example.com/reset?api_key=secret", "/repo"
        )

        assert len(seen) == 1
        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/v1/captures/lookup"
        assert request.url.query == b""
        assert json.loads(request.read()) == {
            "url": "https://example.com/reset?api_key=secret",
            "cwd": "/repo",
        }
        assert result == CapturesLookupResponse(matched=False)


class TestLearnWire:
    """``learn`` resolves the caller's cwd itself -- ``cwd`` is not
    user-supplied on any surface (CLI/MCP/slash/client)."""

    def test_sends_real_cwd(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(202, json={"task_id": "t1", "status": "accepted"})

        _client(handler).learn("a lesson")

        assert json.loads(seen[0].read())["cwd"] == str(Path.cwd())

    def test_degrades_to_empty_cwd_when_cwd_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deleted process cwd must not crash learn -- fall back to ''."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(202, json={"task_id": "t1", "status": "accepted"})

        def _raise() -> Path:
            raise OSError("no such file or directory")

        monkeypatch.setattr(Path, "cwd", _raise)

        result = _client(handler).learn("a lesson")

        assert json.loads(seen[0].read())["cwd"] == ""
        assert result.task_id == "t1"
