"""Tests for quarry.ingestion.web_fetch — HTTP(S) fetch with HTML + SSRF validation."""

from __future__ import annotations

import io
import urllib.request
from http.client import HTTPMessage, HTTPResponse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from quarry.ingestion import web_fetch as wf
from quarry.ingestion.ssrf_redirect import (
    RedirectRejectedError,
    SsrfGuardedRedirectHandler,
)
from quarry.ingestion.web_fetch import WebFetcher

_PUBLIC_ADDR = "93.184.216.34"
_GETADDRINFO = "quarry.url_safety.socket_module.getaddrinfo"


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    """One getaddrinfo record for *ip* (v6 sockaddr is a 4-tuple, v4 a 2-tuple)."""
    family = 10 if ":" in ip else 2
    sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, 1, 6, "", sockaddr)]


@pytest.fixture(autouse=True)
def _resolve_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to a public address by default.

    fetch now gates the final URL's resolved address, so an un-mocked test
    would hit real DNS; individual SSRF tests override this with a blocked IP.
    """
    monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(_PUBLIC_ADDR))


class TestWebFetcher:
    """Test the fetch helper with mocked responses."""

    def test_rejects_non_http(self) -> None:
        with pytest.raises(ValueError, match="Only HTTP"):
            WebFetcher().fetch("ftp://example.com")

    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="Only HTTP"):
            WebFetcher().fetch("file:///etc/passwd")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_fetches_html(self, mock_open: MagicMock) -> None:
        body = b"<html><body><p>Hello</p></body></html>"
        mock_open.return_value = _mock_response(body, "text/html; charset=utf-8")
        assert "Hello" in WebFetcher().fetch("https://example.com")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_uppercase_scheme_accepted(self, mock_open: MagicMock) -> None:
        """An uppercase scheme is accepted, consistent with UrlSafetyCheck."""
        body = b"<html><body><p>OK</p></body></html>"
        mock_open.return_value = _mock_response(body, "text/html")
        assert "OK" in WebFetcher().fetch("HTTP://example.com")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_rejects_non_html_content_type(self, mock_open: MagicMock) -> None:
        mock_open.return_value = _mock_response(b"%PDF-1.4", "application/pdf")
        with pytest.raises(ValueError, match="non-HTML"):
            WebFetcher().fetch("https://example.com/report.pdf")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_accepts_xhtml(self, mock_open: MagicMock) -> None:
        body = b"<html><body><p>XHTML</p></body></html>"
        mock_open.return_value = _mock_response(body, "application/xhtml+xml")
        assert "XHTML" in WebFetcher().fetch("https://example.com")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_content_type_case_insensitive(self, mock_open: MagicMock) -> None:
        body = b"<html><body><p>OK</p></body></html>"
        mock_open.return_value = _mock_response(body, "Text/HTML; charset=UTF-8")
        assert "OK" in WebFetcher().fetch("https://example.com")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_missing_content_type_allowed(self, mock_open: MagicMock) -> None:
        body = b"<html><body><p>No CT</p></body></html>"
        mock_open.return_value = _mock_response(body, "")
        assert "No CT" in WebFetcher().fetch("https://example.com")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_rejects_final_url_non_http(self, mock_open: MagicMock) -> None:
        mock_resp = _mock_response(b"", "text/html")
        mock_resp.url = "ftp://evil.com/file"
        mock_open.return_value = mock_resp
        with pytest.raises(ValueError, match="final URL rejected"):
            WebFetcher().fetch("https://example.com/redirect")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_rejects_final_url_internal_address(
        self, mock_open: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetch that lands on a host resolving to loopback is refused."""
        mock_resp = _mock_response(b"<html></html>", "text/html")
        mock_resp.url = "http://landed.internal/"
        mock_open.return_value = mock_resp
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo("127.0.0.1"))
        with pytest.raises(ValueError, match="final URL rejected"):
            WebFetcher().fetch("https://example.com/redirect")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_http_error_raises_valueerror(self, mock_open: MagicMock) -> None:
        from urllib.error import HTTPError

        mock_open.side_effect = HTTPError(
            "https://example.com/missing",
            404,
            "Not Found",
            {},  # type: ignore[arg-type]
            None,
        )
        with pytest.raises(ValueError, match="HTTP 404"):
            WebFetcher().fetch("https://example.com/missing")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_url_error_raises_oserror(self, mock_open: MagicMock) -> None:
        from urllib.error import URLError

        mock_open.side_effect = URLError("Name or service not known")
        with pytest.raises(OSError, match="Cannot reach"):
            WebFetcher().fetch("https://nonexistent.invalid")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_custom_timeout_passed_through(self, mock_open: MagicMock) -> None:
        body = b"<html><body><p>Hi</p></body></html>"
        mock_open.return_value = _mock_response(body, "text/html")
        WebFetcher(timeout=7).fetch("https://example.com")
        assert mock_open.call_args.kwargs["timeout"] == 7

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_oversize_body_fails_at_the_cap(self, mock_open: MagicMock) -> None:
        """A body past the size cap fails cleanly instead of streaming forever."""
        oversize = b"x" * (wf._MAX_RESPONSE_BYTES + 1)
        mock_open.return_value = _mock_response(oversize, "text/html")
        with pytest.raises(ValueError, match=r"exceeds .*-byte cap"):
            WebFetcher().fetch("https://example.com/huge")

    @patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open")
    def test_slow_drip_aborts_at_the_deadline(
        self, mock_open: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that never ends is aborted once the wall-clock deadline passes.

        The fake clock stays put while the deadline is computed, then jumps past
        it, so the read loop trips ``TimeoutError`` rather than draining a body
        that keeps producing chunks under every per-socket-op timeout.
        """
        drip = _mock_response(b"", "text/html")
        drip.read.side_effect = None
        drip.read.return_value = b"x" * 16  # always more bytes, never EOF
        clock = iter([0.0, 0.0, 1_000_000.0, 1_000_000.0, 1_000_000.0])
        monkeypatch.setattr(wf, "monotonic", lambda: next(clock))
        mock_open.return_value = drip
        # fetch() wraps the deadline TimeoutError with the URL so concurrent
        # fetches are distinguishable in logs.
        with pytest.raises(TimeoutError, match="time budget") as excinfo:
            WebFetcher().fetch("https://example.com/slow")
        assert "https://example.com/slow" in str(excinfo.value)


class TestRedirectGate:
    """The per-hop SSRF gate on redirect targets (SsrfGuardedRedirectHandler)."""

    @pytest.mark.parametrize(
        ("host", "resolved"),
        [
            ("link-local.test", "169.254.169.254"),  # cloud metadata range
            ("loopback4.test", "127.0.0.1"),
            ("loopback6.test", "::1"),
            ("private.test", "10.0.0.5"),  # RFC 1918
            ("private192.test", "192.168.1.10"),  # RFC 1918
            ("cgnat.test", "100.64.1.1"),  # RFC 6598
            ("mapped-ll.test", "::ffff:169.254.169.254"),  # IPv4-mapped link-local
            ("mapped-unspec.test", "::ffff:0.0.0.0"),  # IPv4-mapped unspecified
        ],
    )
    def test_redirect_to_blocked_resolved_address_is_refused(
        self, host: str, resolved: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect whose host RESOLVES to a blocked address is refused.

        The gate runs against the resolved address, so a public-looking hostname
        pointing at an internal IP is caught, and the handler raises before it
        can return a Request — the internal target is never opened.
        """
        seen: list[str] = []

        def _resolver(name: str, *a: Any, **k: Any) -> Any:
            seen.append(name)
            return _addrinfo(resolved)

        monkeypatch.setattr(_GETADDRINFO, _resolver)
        handler = SsrfGuardedRedirectHandler()
        req = urllib.request.Request("https://public.example/start")
        with pytest.raises(RedirectRejectedError, match="redirect target rejected"):
            handler.redirect_request(
                req, io.BytesIO(b""), 302, "Found", HTTPMessage(), f"http://{host}/x"
            )
        assert host in seen  # the gate resolved the redirect target's host

    def test_redirect_to_metadata_ip_is_refused_without_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A literal cloud-metadata IP is blocked by hostname, before any DNS."""
        monkeypatch.setattr(_GETADDRINFO, _boom)
        handler = SsrfGuardedRedirectHandler()
        req = urllib.request.Request("https://public.example/start")
        with pytest.raises(RedirectRejectedError, match="redirect target rejected"):
            handler.redirect_request(
                req,
                io.BytesIO(b""),
                302,
                "Found",
                HTTPMessage(),
                "http://169.254.169.254/latest/meta-data/",
            )

    def test_safe_redirect_is_followed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A redirect to a public address returns a Request for the new URL."""
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(_PUBLIC_ADDR))
        handler = SsrfGuardedRedirectHandler()
        req = urllib.request.Request("https://public.example/start")
        result = handler.redirect_request(
            req,
            io.BytesIO(b""),
            302,
            "Found",
            HTTPMessage(),
            "https://public.example/next",
        )
        assert result is not None
        assert result.full_url == "https://public.example/next"

    def test_fetch_never_opens_internal_redirect_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a public URL that 302s internal fails; internal is never opened.

        A recording handler intercepts every open and returns a 302 to an
        internal host for the start URL.  The guard resolves that host to a
        private address and aborts, so only the start URL is ever opened.
        """
        start = "https://public.example/start"
        internal = "http://internal.service/secret"
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo("10.0.0.9"))
        recorder = _RecordingHandler(start, internal)
        opener = urllib.request.OpenerDirector()
        opener.add_handler(SsrfGuardedRedirectHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(recorder)
        monkeypatch.setattr(wf, "GUARDED_OPENER", opener)
        with pytest.raises(ValueError, match="redirect target rejected"):
            WebFetcher().fetch(start)
        assert recorder.opened == [start]  # internal target never fetched


class TestGuardedOpenerProxy:
    """GUARDED_OPENER must not honor env proxies (would route around the gate)."""

    def test_env_proxy_is_not_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HTTP(S)_PROXY set, the guarded opener routes to no proxy.

        The gate resolves and checks the TARGET host; if urllib honored an env
        proxy the socket would instead connect to the proxy -- a host the gate
        never vetted -- reintroducing SSRF via an internal/attacker proxy.
        """
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
        monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
        # Sanity: urllib does see the env proxy, and a DEFAULT opener would use it.
        assert urllib.request.getproxies().get("http") == "http://10.0.0.1:3128"

        def _routes_to_proxy(opener: urllib.request.OpenerDirector) -> bool:
            # .handlers / ProxyHandler.proxies are runtime attrs absent from
            # typeshed; read them via getattr to stay type-clean.
            handlers = getattr(opener, "handlers", [])
            return any(
                isinstance(h, urllib.request.ProxyHandler)
                and getattr(h, "proxies", None)
                for h in handlers
            )

        # Sanity: a DEFAULT opener WOULD route to the env proxy.
        assert _routes_to_proxy(urllib.request.build_opener())
        # The guarded opener honors no proxy -- direct, gated connection only.
        assert not _routes_to_proxy(SsrfGuardedRedirectHandler.build_opener())


def _boom(*_a: Any, **_k: Any) -> Any:
    """A resolver that must never be called."""
    raise AssertionError("getaddrinfo should not be called for a literal metadata IP")


class _FakeResp:
    """Minimal response object for a fake urllib handler."""

    def __init__(self, code: int, headers: HTTPMessage, url: str) -> None:
        self.code = code
        self.msg = "Found" if code == 302 else "OK"
        self._headers = headers
        self.url = url

    def info(self) -> HTTPMessage:
        return self._headers

    def read(self, *_a: Any) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


class _RecordingHandler(urllib.request.BaseHandler):
    """Intercept opens: 302 the start URL to *internal*, record every URL."""

    def __init__(self, start: str, internal: str) -> None:
        self._start = start
        self._internal = internal
        self.opened: list[str] = []

    def _open(self, req: urllib.request.Request) -> _FakeResp:
        url = req.full_url
        self.opened.append(url)
        headers = HTTPMessage()
        if url == self._start:
            headers["Location"] = self._internal
            return _FakeResp(302, headers, url)
        headers["Content-Type"] = "text/html"
        return _FakeResp(200, headers, url)

    def http_open(self, req: urllib.request.Request) -> _FakeResp:
        return self._open(req)

    def https_open(self, req: urllib.request.Request) -> _FakeResp:
        return self._open(req)


def _mock_response(
    body: bytes,
    content_type: str,
    final_url: str = "https://example.com",
) -> MagicMock:
    """Create a mock HTTP response yielding *body* once, then EOF.

    ``read`` returns the whole body on its first chunked call and an empty
    ``bytes`` afterwards, matching the fetcher's read-to-EOF loop.
    """
    mock_resp = MagicMock(spec=HTTPResponse)
    mock_resp.read.side_effect = [body, b""]
    mock_resp.headers = _make_headers(content_type)
    # urllib sets ``.url`` on the response at runtime; it is not a class
    # attribute, so a spec'd mock only exposes it once assigned explicitly.
    mock_resp.url = final_url
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_headers(content_type: str) -> Any:
    """Create a mock headers object with Content-Type."""
    msg = HTTPMessage()
    if content_type:
        msg["Content-Type"] = content_type
    return msg
