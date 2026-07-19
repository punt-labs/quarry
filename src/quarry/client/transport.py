"""The injectable HTTP transport seam and its production ``httpx`` implementation.

:class:`Transport` is the one narrow surface :class:`~quarry.client.client.QuarryClient`
depends on, so tests inject an ``ASGITransport`` over the real daemon app while
production uses :class:`HttpxTransport` — a single long-lived ``httpx.Client``
carrying the pinned-CA TLS context and the ``Authorization: Bearer`` header
absorbed from the mcp-proxy config.  A refused or broken connection surfaces as a
typed :class:`~quarry.client.errors.QuarryConnectionError`, never a bare
``OSError`` or ``SystemExit``.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, Self, final

import httpx

from quarry.client.errors import QuarryConnectionError, QuarryError
from quarry.remote import to_netloc, ws_to_http

_DEFAULT_TIMEOUT = 15.0
_MAX_CONNECT_RETRIES = 1
_HTTP_MULTIPLE_CHOICES = 300
_PREVIEW_BYTES = 200


@dataclass(frozen=True, slots=True)
class Response:
    """A parsed HTTP response: its status and decoded JSON body (a wire boundary)."""

    _status: int
    _json: object  # wire boundary — a decoded JSON value, narrowed by the caller

    @property
    def status(self) -> int:
        return self._status

    @property
    def json_body(self) -> object:
        return self._json


class Transport(Protocol):
    """The one method :class:`QuarryClient` needs: issue a request, return a body."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Response: ...


@final
class HttpxTransport:
    """Production transport: one long-lived ``httpx.Client`` with CA-pin + bearer."""

    _client: httpx.Client

    def __new__(cls, client: httpx.Client) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> Self:
        """Build a transport from a ``{url, ca_cert?, headers?}`` client config.

        Raises :class:`QuarryConnectionError` when a ``wss://`` target has no
        pinned CA or the CA cannot be loaded — the failure predates any request,
        so it is a connection error, not a server one.
        """
        base_url, verify, headers = cls._connection(mapping)
        client = httpx.Client(
            base_url=base_url,
            headers=headers,
            verify=verify,
            timeout=_DEFAULT_TIMEOUT,
            # Never consult HTTP(S)_PROXY/ALL_PROXY or netrc: the client talks
            # only to a known quarry daemon at an explicit URL. Honoring env
            # proxies would route the request — including the loopback
            # Authorization: Bearer serve.token — through a proxy, leaking it.
            trust_env=False,
        )
        return cls(client)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Issue *method* *path* and return the parsed response.

        A refused connection is retried once (the mcp-proxy reconnect policy)
        before surfacing as :class:`QuarryConnectionError`; any non-2xx status
        raises the classified :class:`QuarryError`.
        """
        target = str(self._client.base_url)
        # Explicit None check, not `or`: a caller's intentional 0/0.0 (a
        # zero/immediate timeout) must be honored, not swapped for the default.
        effective_timeout = _DEFAULT_TIMEOUT if timeout is None else timeout
        last_exc: httpx.HTTPError | None = None
        for _ in range(_MAX_CONNECT_RETRIES + 1):
            try:
                resp = self._client.request(
                    method,
                    path,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body is not None else None,
                    timeout=effective_timeout,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_exc = exc
                continue
            except httpx.HTTPError as exc:
                raise QuarryConnectionError(
                    f"Cannot reach remote quarry server at {target}: {exc}", target
                ) from exc
            return self._parse(resp)
        raise QuarryConnectionError(
            f"Cannot connect to remote quarry server at {target}: {last_exc}", target
        ) from last_exc

    def close(self) -> None:
        """Close the underlying HTTP client and release its connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @classmethod
    def _parse(cls, resp: httpx.Response) -> Response:
        """Turn an ``httpx`` response into a :class:`Response`, raising on error."""
        raw = resp.content
        status = resp.status_code
        parsed_ok, body = cls._try_json(raw)
        if status >= _HTTP_MULTIPLE_CHOICES:
            # Truncate a non-JSON error body (e.g. a large HTML gateway page) so a
            # big/hostile response cannot flood stderr or expose its full contents.
            fallback = {"error": raw[:_PREVIEW_BYTES].decode("utf-8", "replace")}
            raise QuarryError.from_response(status, body if parsed_ok else fallback)
        if not parsed_ok:
            if not raw:
                return Response(status, {})
            preview = raw[:_PREVIEW_BYTES].decode("utf-8", "replace")
            raise QuarryError(
                f"Malformed response from remote server: expected JSON, got {preview!r}"
            )
        return Response(status, body)

    @staticmethod
    def _try_json(raw: bytes) -> tuple[bool, object]:
        """Return ``(True, value)`` on a JSON decode, else ``(False, None)``."""
        if not raw:
            return False, None
        try:
            return True, json.loads(raw)
        except ValueError:
            return False, None

    @classmethod
    def _connection(
        cls, mapping: Mapping[str, object]
    ) -> tuple[str, ssl.SSLContext | bool, dict[str, str]]:
        """Return the base URL, TLS verify context, and headers for *mapping*."""
        raw_url = str(mapping["url"])
        parsed = urllib.parse.urlparse(ws_to_http(raw_url))
        scheme = parsed.scheme or "http"
        # Fail closed: a hostless URL (e.g. "ws://:9000") must NOT default to
        # localhost — that would send Authorization: Bearer to loopback. The
        # resolver validates upstream; this is the defense-in-depth boundary.
        host = parsed.hostname
        if not host:
            raise QuarryConnectionError(
                f"Invalid server URL has no host: {raw_url!r}", raw_url
            )
        port = parsed.port or 8420
        base_url = f"{scheme}://{to_netloc(host, port)}"
        headers = cls._headers(mapping)
        if scheme != "https":
            return base_url, True, headers
        return base_url, cls._pinned_context(mapping, base_url), headers

    @staticmethod
    def _headers(mapping: Mapping[str, object]) -> dict[str, str]:
        """Return the request headers, carrying the bearer token when present."""
        raw = mapping.get("headers")
        if not isinstance(raw, Mapping):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    @staticmethod
    def _pinned_context(mapping: Mapping[str, object], target: str) -> ssl.SSLContext:
        """Build a TLS context trusting only the pinned CA (no system roots).

        Excludes the system trust store entirely so a system-trusted cert cannot
        satisfy verification and defeat pinning.  A missing or unloadable CA is a
        :class:`QuarryConnectionError`, not a ``SystemExit``.
        """
        ca_cert = mapping.get("ca_cert")
        if not ca_cert:
            raise QuarryConnectionError(
                "Remote server uses HTTPS but no CA cert is pinned. "
                "Run 'quarry login' to trust the server's certificate.",
                target,
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            ctx.load_verify_locations(str(ca_cert))
        except (OSError, ssl.SSLError) as exc:
            raise QuarryConnectionError(
                f"Cannot load CA certificate {ca_cert!r}. "
                f"Run 'quarry login' to reconfigure. ({exc})",
                target,
            ) from exc
        return ctx
