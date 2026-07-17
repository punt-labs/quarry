"""Class-2 boundary tests: every non-2xx status and malformed body raises a typed
:class:`QuarryError`, never a raw httpx/JSON exception or ``SystemExit``.

Drives the real :class:`HttpxTransport` parse/classify path through
``httpx.MockTransport`` so the wire-status → error-leaf mapping is exercised
end-to-end, not stubbed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from quarry.client.errors import (
    AuthError,
    BadRequestError,
    NotFoundError,
    ProtocolError,
    QuarryConnectionError,
    QuarryError,
    ServerError,
)
from quarry.client.transport import HttpxTransport

_Handler = Callable[[httpx.Request], httpx.Response]


def _transport(handler: _Handler) -> HttpxTransport:
    client = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    return HttpxTransport(client)


def _responds(status: int, body: object) -> _Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


class TestStatusClassification:
    def test_401_raises_auth_error(self) -> None:
        with pytest.raises(AuthError) as info:
            _transport(_responds(401, {"error": "unauthorized"})).request("GET", "/x")
        assert info.value.message == "unauthorized"

    def test_404_raises_not_found_with_status(self) -> None:
        with pytest.raises(NotFoundError) as info:
            _transport(_responds(404, {"error": "missing"})).request("GET", "/x")
        assert info.value.status == 404
        assert info.value.message == "missing"

    def test_409_raises_bad_request_carrying_task_id(self) -> None:
        body = {"error": "Sync already in progress", "task_id": "T42"}
        with pytest.raises(BadRequestError) as info:
            _transport(_responds(409, body)).request("GET", "/x")
        assert info.value.status == 409
        assert info.value.task_id == "T42"

    def test_500_raises_server_error(self) -> None:
        with pytest.raises(ServerError) as info:
            _transport(_responds(500, {"error": "boom"})).request("GET", "/x")
        assert info.value.status == 500

    def test_error_body_without_error_key_gets_generic_message(self) -> None:
        with pytest.raises(ServerError) as info:
            _transport(_responds(503, {"detail": "x"})).request("GET", "/x")
        assert info.value.message == "HTTP 503"


class TestMalformedBody:
    def test_non_json_2xx_raises_protocol_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        with pytest.raises(ProtocolError):
            _transport(handler).request("GET", "/x")

    def test_empty_2xx_body_is_empty_object(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        resp = _transport(handler).request("GET", "/x")
        assert resp.json_body == {}

    def test_non_json_error_body_still_classifies_by_status(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"plain text")

        with pytest.raises(NotFoundError):
            _transport(handler).request("GET", "/x")


class TestConnectionFailures:
    def test_connect_error_raises_connection_error_not_oserror(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(QuarryConnectionError) as info:
            _transport(handler).request("GET", "/x")
        assert "http://test" in info.value.target

    def test_read_timeout_raises_connection_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        with pytest.raises(QuarryConnectionError):
            _transport(handler).request("GET", "/x")

    def test_wss_without_pinned_ca_raises_connection_error_not_systemexit(self) -> None:
        # The Class-2 fix: a missing pinned CA is a typed connection error, never
        # a SystemExit escaping the library tier.
        with pytest.raises(QuarryConnectionError) as info:
            HttpxTransport.from_mapping({"url": "wss://example.com:8420"})
        assert "CA" in info.value.message

    def test_wss_with_unloadable_ca_raises_connection_error(
        self, tmp_path: Path
    ) -> None:
        bad_ca = str(tmp_path / "does-not-exist.crt")
        with pytest.raises(QuarryConnectionError):
            HttpxTransport.from_mapping(
                {"url": "wss://example.com:8420", "ca_cert": bad_ca}
            )


class TestFromResponseUnit:
    def test_base_never_returned_directly(self) -> None:
        # Every classified result is a concrete leaf, never the base class.
        for status in (400, 401, 404, 409, 413, 415, 422, 500, 503):
            err = QuarryError.from_response(status, {"error": "x"})
            assert type(err) is not QuarryError
            assert err.message
