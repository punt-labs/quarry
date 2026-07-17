"""Class-2 boundary tests: every non-2xx status and malformed body raises a typed
error, never a raw httpx/JSON exception or ``SystemExit``.

Drives the real :class:`HttpxTransport` parse/classify path through
``httpx.MockTransport`` so the wire-status → error mapping is exercised
end-to-end.  A non-2xx status becomes an :class:`HttpError` carrying the wire
``status`` (the CLI dispatches on that, and 409 carries the running ``task_id``);
a socket failure becomes :class:`QuarryConnectionError`; a malformed 2xx body
becomes a base :class:`QuarryError`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from quarry.client.errors import (
    HttpError,
    QuarryConnectionError,
    QuarryError,
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
    def test_401_raises_http_error_with_status(self) -> None:
        with pytest.raises(HttpError) as info:
            _transport(_responds(401, {"error": "unauthorized"})).request("GET", "/x")
        assert info.value.status == 401
        assert info.value.message == "unauthorized"

    def test_404_raises_http_error_with_status(self) -> None:
        with pytest.raises(HttpError) as info:
            _transport(_responds(404, {"error": "missing"})).request("GET", "/x")
        assert info.value.status == 404
        assert info.value.message == "missing"

    def test_409_carries_task_id(self) -> None:
        body = {"error": "Sync already in progress", "task_id": "T42"}
        with pytest.raises(HttpError) as info:
            _transport(_responds(409, body)).request("GET", "/x")
        assert info.value.status == 409
        assert info.value.task_id == "T42"

    def test_500_raises_http_error_with_status(self) -> None:
        with pytest.raises(HttpError) as info:
            _transport(_responds(500, {"error": "boom"})).request("GET", "/x")
        assert info.value.status == 500

    def test_error_body_without_error_key_gets_generic_message(self) -> None:
        with pytest.raises(HttpError) as info:
            _transport(_responds(503, {"detail": "x"})).request("GET", "/x")
        assert info.value.message == "HTTP 503"


class TestMalformedBody:
    def test_non_json_2xx_raises_quarry_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        with pytest.raises(QuarryError) as info:
            _transport(handler).request("GET", "/x")
        # A malformed 2xx is a base error, not an HTTP-status error.
        assert not isinstance(info.value, HttpError)

    def test_empty_2xx_body_is_empty_object(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        resp = _transport(handler).request("GET", "/x")
        assert resp.json_body == {}

    def test_non_json_error_body_still_classifies_by_status(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"plain text")

        with pytest.raises(HttpError) as info:
            _transport(handler).request("GET", "/x")
        assert info.value.status == 404


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


class TestProxyIsolation:
    def test_client_never_routes_through_an_env_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With HTTP_PROXY/ALL_PROXY set, the client must connect DIRECTLY to the
        # daemon — never route the request (and its loopback Bearer serve.token)
        # through a proxy. trust_env is off, so no env proxy/netrc is consulted.
        monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:3128")
        monkeypatch.setenv("ALL_PROXY", "http://attacker.example:3128")
        transport = HttpxTransport.from_mapping(
            {"url": "ws://127.0.0.1:8420", "headers": {"Authorization": "Bearer t"}}
        )
        assert transport._client.trust_env is False
        # No proxy mounts were derived from the environment.
        assert transport._client._mounts == {}


class TestTimeout:
    def test_zero_timeout_is_honored_not_swapped_for_default(self) -> None:
        # An intentional 0/immediate timeout must reach the client, not be
        # replaced by the default via a falsy `or`.
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, json={})

        _transport(handler).request("GET", "/x", timeout=0)
        recorded = seen["timeout"]
        assert isinstance(recorded, dict)
        assert all(value == 0 for value in recorded.values())


class TestFromResponseUnit:
    def test_always_returns_http_error_leaf(self) -> None:
        for status in (400, 401, 404, 409, 413, 415, 422, 500, 503):
            err = QuarryError.from_response(status, {"error": "x"})
            assert isinstance(err, HttpError)
            assert err.status == status
            assert err.message == "x"
