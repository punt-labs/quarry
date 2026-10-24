"""Tests for the gated USP web client — SSRF gate at USP's fetch boundary.

These drive USP's REAL recursion (sitemap-index, robots.txt, redirects) through
the injected client, so an internal URL discovered mid-crawl is proven never
fetched — the bypass a leaf-only test would hide.
"""

from __future__ import annotations

import io
import logging
import threading
import urllib.request
from collections.abc import Generator
from email.message import Message
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from usp.web_client.abstract_client import WebClientErrorResponse

import quarry.sitemap_web_client as swc
from quarry.http_url import HttpUrl
from quarry.ingestion.ssrf_redirect import SsrfGuardedRedirectHandler
from quarry.sitemap import SitemapDiscovery
from quarry.sitemap_web_client import GatedSitemapWebClient

_GETADDRINFO = "quarry.url_safety.socket_module.getaddrinfo"
_PUBLIC = "93.184.216.34"


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    family = 10 if ":" in ip else 2
    sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, 1, 6, "", sockaddr)]


def _resolver(mapping: dict[str, str]):
    def _f(host: str, *_a: Any, **_k: Any) -> Any:
        return _addrinfo(mapping.get(host, _PUBLIC))

    return _f


def _index_xml(*locs: str) -> bytes:
    inner = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{inner}</sitemapindex>"
    ).encode()


def _urlset_xml(*locs: str) -> bytes:
    inner = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{inner}</urlset>"
    ).encode()


class _FakeResp:
    """A minimal http.client.HTTPResponse stand-in for the fake opener."""

    def __init__(self, url: str, body: bytes) -> None:
        self.status = 200
        self.reason = "OK"
        self.url = url
        self._body = body
        headers = HTTPMessage()
        headers["Content-Type"] = "application/xml"
        self.headers = headers

    def read(self, amt: int | None = None) -> bytes:
        return self._body if amt is None else self._body[:amt]

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


class _FakeOpener:
    """Serve canned bodies by URL and record every URL opened; 404 otherwise."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self._routes = routes
        self.opened: list[str] = []

    def open(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResp:
        url = request.full_url
        self.opened.append(url)
        body = self._routes.get(url)
        if body is None:
            raise HTTPError(url, 404, "Not Found", Message(), None)
        return _FakeResp(url, body)


class _LandingOpener:
    """Return a response whose final url differs from the requested url."""

    def __init__(self, landed: str) -> None:
        self._landed = landed

    def open(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResp:
        return _FakeResp(self._landed, _urlset_xml("http://internal.example/x"))


class _TimeoutOpener:
    """An opener whose fetch times out at the transport layer."""

    def open(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResp:
        raise TimeoutError("read timed out")


class _TrackedFp(io.BytesIO):
    """A response body that records whether it was closed."""

    closed_count: int = 0

    def close(self) -> None:
        type(self).closed_count += 1
        super().close()


class _HttpErrorOpener:
    """An opener that raises an HTTPError carrying an open (fd-holding) body."""

    def __init__(self, code: int) -> None:
        self._code = code

    def open(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResp:
        fp = _TrackedFp(b"error body")
        raise HTTPError(request.full_url, self._code, "err", HTTPMessage(), fp)


class _BrokenReadResp:
    """A response whose body read raises IncompleteRead mid-stream."""

    def __init__(self, url: str) -> None:
        self.status = 200
        self.reason = "OK"
        self.url = url
        headers = HTTPMessage()
        headers["Content-Type"] = "application/xml"
        self.headers = headers

    def read(self, amt: int | None = None) -> bytes:
        from http.client import IncompleteRead

        raise IncompleteRead(b"partial")

    def __enter__(self) -> _BrokenReadResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


_BLOCKED_CLASSES = [
    ("link-local", "169.254.169.254"),
    ("loopback-v4", "127.0.0.1"),
    ("loopback-v6", "::1"),
    ("rfc1918", "10.0.0.7"),
    ("rfc1918-192", "192.168.1.5"),
    ("cgnat", "100.64.2.3"),
    ("ipv4-mapped-cgnat", "::ffff:100.64.2.3"),
    ("ipv4-mapped-loopback", "::ffff:127.0.0.1"),
    ("ipv4-mapped-link-local", "::ffff:169.254.169.254"),
    ("ipv4-mapped-unspecified", "::ffff:0.0.0.0"),
]


class TestGatedClientGet:
    """GatedSitemapWebClient.get gates before any fetch, fail-closed."""

    @pytest.mark.parametrize(("label", "resolved"), _BLOCKED_CLASSES)
    def test_blocked_url_never_fetched(
        self, label: str, resolved: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(resolved))
        opener = _FakeOpener({})
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        resp = GatedSitemapWebClient().get("https://listed.attacker.test/s.xml")
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is False
        assert opener.opened == []  # gate ran before any outbound request

    def test_metadata_ip_blocked_without_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("no DNS for a literal metadata IP")

        monkeypatch.setattr(_GETADDRINFO, _boom)
        opener = _FakeOpener({})
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        resp = GatedSitemapWebClient().get("http://169.254.169.254/latest/")
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert opener.opened == []

    def test_multi_record_dns_blocks_if_any_internal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host resolving to [public, private] is rejected (all records checked)."""
        records = _addrinfo(_PUBLIC) + _addrinfo("10.0.0.9")
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: records)
        opener = _FakeOpener({})
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        resp = GatedSitemapWebClient().get("https://mixed.attacker.test/s.xml")
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert opener.opened == []

    def test_body_read_error_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body-read failure (IncompleteRead) is returned, never raised."""
        url = "https://safe.example/sitemap.xml"

        class _BrokenReadOpener:
            def open(self, request: Any, timeout: float | None = None) -> Any:
                return _BrokenReadResp(url)

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _BrokenReadOpener())
        resp = GatedSitemapWebClient().get(url)
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is False

    def test_safe_url_is_fetched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        url = "https://safe.example/sitemap.xml"
        opener = _FakeOpener({url: _urlset_xml("https://safe.example/p1")})
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        resp = GatedSitemapWebClient().get(url)
        from usp.web_client.abstract_client import AbstractWebClientSuccessResponse

        assert isinstance(resp, AbstractWebClientSuccessResponse)
        assert resp.status_code() == 200
        assert resp.status_message() == "OK"
        assert b"safe.example/p1" in resp.raw_data()
        assert opener.opened == [url]

    def test_final_url_resolving_internal_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetch whose FINAL url resolves internal is refused, non-retryable.

        The initial url passes the gate, but the response lands on a host that
        resolves to a private address — the final-url gate must catch it.
        """
        start = "https://safe.example/sitemap.xml"
        landed = "http://internal.example/final.xml"
        monkeypatch.setattr(
            _GETADDRINFO,
            _resolver({"safe.example": _PUBLIC, "internal.example": "10.0.0.9"}),
        )
        monkeypatch.setattr(swc, "GUARDED_OPENER", _LandingOpener(landed))
        resp = GatedSitemapWebClient().get(start)
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is False

    def test_network_timeout_is_retryable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transport timeout is reported retryable, never raised."""
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _TimeoutOpener())
        resp = GatedSitemapWebClient().get("https://safe.example/slow.xml")
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is True

    def test_http_error_closes_response_fd(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every HTTPError-returning fetch closes the response fd (no leak).

        HTTPError IS an open response holding a socket fd; leaking one per
        failed fetch would exhaust fds over a crawl (EMFILE -> starvation).
        """
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _HttpErrorOpener(500))
        _TrackedFp.closed_count = 0
        for _ in range(20):
            resp = GatedSitemapWebClient().get("https://safe.example/x.xml")
            assert isinstance(resp, WebClientErrorResponse)
        assert _TrackedFp.closed_count == 20  # one close per failed fetch

    def test_error_response_readable_after_fd_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """message() and retryable() are readable AFTER the fd is closed.

        Capture-before-close: USP logs response.message() after get() returns,
        so the returned response must expose plain captured values, never a
        lazy read of the now-closed HTTPError response.
        """
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _HttpErrorOpener(404))
        resp = GatedSitemapWebClient().get("https://safe.example/x.xml")
        assert isinstance(resp, WebClientErrorResponse)
        # Readable with no "I/O operation on closed file" -- values were captured
        # before exc.close().
        assert resp.message() == "404 Not Found"
        assert resp.retryable() is False

    @pytest.mark.parametrize("code", [500, 503, 429])
    def test_transient_http_errors_are_retryable(
        self, code: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5xx and 429 (rate limit) are transient, so the fetch is retryable."""
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _HttpErrorOpener(code))
        resp = GatedSitemapWebClient().get("https://safe.example/x.xml")
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is True

    @pytest.mark.parametrize("code", [400, 403, 404])
    def test_permanent_http_errors_are_not_retryable(
        self, code: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """4xx (other than 429) are permanent, so the fetch is not retried."""
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _HttpErrorOpener(code))
        resp = GatedSitemapWebClient().get("https://safe.example/x.xml")
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is False

    def test_ssrf_block_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An SSRF-blocked URL is never retried (and never opened)."""
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo("10.0.0.9"))
        resp = GatedSitemapWebClient().get("https://internal.example/x.xml")
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.retryable() is False


class TestUspRecursionGated:
    """USP's real recursion is gated: internal index/robots targets never fetched."""

    def test_sitemap_index_internal_subsitemap_never_fetched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        index = "https://safe.example/sitemap_index.xml"
        safe_sub = "https://safe.example/sub-safe.xml"
        evil_sub = "http://internal.example/sub-evil.xml"
        routes = {
            index: _index_xml(safe_sub, evil_sub),
            safe_sub: _urlset_xml("https://safe.example/page1"),
            # evil_sub is intentionally routable: if the gate failed, the crawl
            # would fetch it. The gate must stop USP before it is opened.
            evil_sub: _urlset_xml("http://internal.example/secret"),
        }
        opener = _FakeOpener(routes)
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        monkeypatch.setattr(
            _GETADDRINFO,
            _resolver({"safe.example": _PUBLIC, "internal.example": "10.0.0.9"}),
        )

        entries = SitemapDiscovery.discover_urls(HttpUrl(index))

        locs = [e.loc for e in entries]
        assert "https://safe.example/page1" in locs
        assert evil_sub not in opener.opened  # internal sub-sitemap never fetched
        assert "http://internal.example/secret" not in locs

    def test_robots_sitemap_line_to_internal_never_fetched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots = "https://safe.example/robots.txt"
        safe_sm = "https://safe.example/known.xml"
        evil_sm = "http://internal.example/evil.xml"
        routes = {
            robots: f"Sitemap: {safe_sm}\nSitemap: {evil_sm}\n".encode(),
            safe_sm: _urlset_xml("https://safe.example/pageA"),
            evil_sm: _urlset_xml("http://internal.example/secret"),
        }
        opener = _FakeOpener(routes)
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        monkeypatch.setattr(
            _GETADDRINFO,
            _resolver({"safe.example": _PUBLIC, "internal.example": "10.0.0.9"}),
        )

        entries = SitemapDiscovery.discover_pages(HttpUrl("https://safe.example/docs"))

        locs = [e.loc for e in entries]
        assert evil_sm not in opener.opened  # robots-listed internal never fetched
        assert "http://internal.example/secret" not in locs
        assert "https://safe.example/pageA" in locs

    def test_safe_multilevel_index_still_crawls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        top = "https://safe.example/index.xml"
        mid = "https://safe.example/mid.xml"
        leaf = "https://safe.example/leaf.xml"
        routes = {
            top: _index_xml(mid),
            mid: _index_xml(leaf),
            leaf: _urlset_xml("https://safe.example/final"),
        }
        opener = _FakeOpener(routes)
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))

        entries = SitemapDiscovery.discover_urls(HttpUrl(top))

        assert [e.loc for e in entries] == ["https://safe.example/final"]


class TestRedirectOnSitemapFetch:
    """A 302 on the sitemap fetch to an internal address is refused."""

    def test_sitemap_302_to_internal_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = "https://safe.example/sitemap.xml"
        internal = "http://internal.example/secret.xml"
        recorder = _RedirectRecorder(start, internal)
        opener = urllib.request.OpenerDirector()
        opener.add_handler(SsrfGuardedRedirectHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(recorder)
        monkeypatch.setattr(swc, "GUARDED_OPENER", opener)
        monkeypatch.setattr(_GETADDRINFO, _resolver({"internal.example": "10.0.0.9"}))

        resp = GatedSitemapWebClient().get(start)
        from usp.web_client.abstract_client import WebClientErrorResponse

        assert isinstance(resp, WebClientErrorResponse)
        assert recorder.opened == [start]  # redirect target never opened


class _RedirectRecorder(urllib.request.BaseHandler):
    """302 the start URL to *internal*; record every URL opened."""

    def __init__(self, start: str, internal: str) -> None:
        self._start = start
        self._internal = internal
        self.opened: list[str] = []

    def _open(self, req: urllib.request.Request) -> Any:
        url = req.full_url
        self.opened.append(url)
        headers = HTTPMessage()
        if url == self._start:
            headers["Location"] = self._internal
            return _RedirectResp(302, headers, url)
        headers["Content-Type"] = "application/xml"
        return _RedirectResp(200, headers, url)

    def http_open(self, req: urllib.request.Request) -> Any:
        return self._open(req)

    def https_open(self, req: urllib.request.Request) -> Any:
        return self._open(req)


class _RedirectResp:
    def __init__(self, code: int, headers: HTTPMessage, url: str) -> None:
        self.status = code
        self.code = code
        self.msg = "Found" if code == 302 else "OK"
        self.reason = self.msg
        self.url = url
        self._headers = headers

    def info(self) -> HTTPMessage:
        return self._headers

    @property
    def headers(self) -> HTTPMessage:
        return self._headers

    def read(self, amt: int | None = None) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> _RedirectResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


def _open_fd_count() -> int:
    """Return the number of file descriptors this process currently holds."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        path = Path(fd_dir)
        if path.is_dir():
            return sum(1 for _ in path.iterdir())
    pytest.skip("no /proc/self/fd or /dev/fd on this platform")


class _FailingHandler(BaseHTTPRequestHandler):
    """Loopback server: 404 on /err, a 302 to a metadata IP on /redir.

    404 is non-retryable, so USP issues exactly ONE request per fetch -- no
    retry storm (a 5xx would be retryable and multiply the real round-trips).
    """

    def do_GET(self) -> None:
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Silence the per-request stderr logging (matches the base signature)."""


@pytest.fixture()
def http_server() -> Generator[HTTPServer]:
    """A real loopback HTTP server that fails every request (404 or 302)."""
    server = HTTPServer(("127.0.0.1", 0), _FailingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


# Real-socket OS-fd invariant test: it needs a live loopback HTTP server, which
# is CI-network-fragile (connection hangs on the runner), so it is tiered OUT of
# the fast CI unit suite (-m "not slow", via the slow marker) and runs in the
# integration tier instead.  The deterministic close-count unit tests above are
# the CI-tier fd guard.
@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.resource
def test_failing_fetches_do_not_leak_real_fds(
    http_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N failing fetches over a REAL socket hold the process fd count flat.

    Covers both fd-bearing error paths against a real server: the HTTPError
    path (a 404 response holding a real socket fd, closed via ``exc.close()``)
    and the RedirectRejectedError path (a real 302 intermediate response whose
    fp is closed in the redirect handler).  404 is non-retryable, so each fetch
    is exactly one round-trip -- no retry storm.  The SSRF gate correctly blocks
    loopback, so the resolver is mocked public to let the connection through to
    the real 127.0.0.1 server; urllib still opens a real socket per request.  A
    1-fd-per-fetch leak over 200 fetches is delta ~200, far above the tolerance,
    so this detects a leak just as surely as a larger count while staying fast.
    """
    monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(_PUBLIC))
    base = f"http://127.0.0.1:{http_server.server_port}"
    client = GatedSitemapWebClient()

    for _ in range(5):  # warm up connection/thread fds before sampling
        client.get(f"{base}/err")
        client.get(f"{base}/redir")

    before = _open_fd_count()
    for _ in range(100):
        assert isinstance(client.get(f"{base}/err"), WebClientErrorResponse)
        assert isinstance(client.get(f"{base}/redir"), WebClientErrorResponse)
    after = _open_fd_count()

    assert after - before <= 5, (
        f"fd leak across 200 failing fetches: {before} -> {after} "
        f"(delta {after - before}); a failed fetch is not releasing its fd"
    )


class TestErrorMessagesAreUrlFree:
    """USP logs the error response message, so it must not quote the URL."""

    _URL = "https://safe.example/s.xml?token=SECRET"

    def test_an_unreachable_host_is_reported_by_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib.error import URLError

        class _Opener:
            def open(self, request: Any, timeout: float | None = None) -> Any:
                raise URLError(f"refused {self._url}")

            _url = TestErrorMessagesAreUrlFree._URL

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _Opener())
        resp = GatedSitemapWebClient().get(self._URL)
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.message() == "fetch failed: cannot reach the host"

    def test_a_transport_failure_is_reported_by_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Opener:
            def open(self, request: Any, timeout: float | None = None) -> Any:
                return _BrokenReadResp(TestErrorMessagesAreUrlFree._URL)

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _Opener())
        resp = GatedSitemapWebClient().get(self._URL)
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.message() == "fetch failed: IncompleteRead"


class TestFailureClassification:
    """Only the fetch refusals are answered; any other exception is a bug."""

    _URL = "https://safe.example/sitemap.xml?token=SECRET"

    def _client_with_opener_raising(
        self, monkeypatch: pytest.MonkeyPatch, failure: BaseException
    ) -> GatedSitemapWebClient:
        class _Opener:
            def open(self, request: Any, timeout: float | None = None) -> Any:
                raise failure

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _Opener())
        return GatedSitemapWebClient()

    def test_a_value_error_from_the_opener_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client_with_opener_raising(monkeypatch, ValueError("a bug"))
        with pytest.raises(ValueError, match="a bug"):
            client.get(self._URL)

    def test_an_os_error_is_an_error_response_and_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = self._client_with_opener_raising(
            monkeypatch, ConnectionResetError(f"reset {self._URL}")
        )
        with caplog.at_level("WARNING"):
            resp = client.get(self._URL)
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.message() == "fetch failed: ConnectionResetError"
        assert "ConnectionResetError" in caplog.text
        assert "https://safe.example/…(segments=1)" in caplog.text
        assert "SECRET" not in caplog.text

    def test_a_policy_rejection_is_an_error_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.url_safety import UrlRejectedError

        client = self._client_with_opener_raising(
            monkeypatch, UrlRejectedError("host resolves to a blocked address")
        )
        resp = client.get(self._URL)
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.message() == "URL rejected: host resolves to a blocked address"


class TestInternationalizedUrl:
    def test_an_iri_reaches_the_opener_as_ascii(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        class _Opener:
            def open(self, request: Any, timeout: float | None = None) -> Any:
                seen.append(request.full_url)
                raise OSError

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _Opener())
        GatedSitemapWebClient().get("https://safe.example/caf\u00e9/\u65e5")
        assert seen == ["https://safe.example/caf%C3%A9/%E6%97%A5"]
        assert seen[0].isascii()


class TestEveryBranchIsClassifiedOnce:
    """HTTPError, timeout and unreachable go through FetchRefusedError too."""

    _URL = "https://safe.example/sitemap.xml?token=SECRET"

    @pytest.mark.parametrize(
        ("failure", "message", "retryable"),
        [
            (
                HTTPError("https://safe.example/x", 503, "boom", HTTPMessage(), None),
                "fetch failed: HTTP 503",
                True,
            ),
            (
                HTTPError("https://safe.example/x", 429, "slow", HTTPMessage(), None),
                "fetch failed: HTTP 429",
                True,
            ),
            (
                HTTPError("https://safe.example/x", 404, "gone", HTTPMessage(), None),
                "404 Not Found",
                False,
            ),
            (TimeoutError("slow"), "fetch failed: timed out", True),
            (
                URLError("dns"),
                "fetch failed: cannot reach the host",
                True,
            ),
            (
                ConnectionResetError("reset"),
                "fetch failed: ConnectionResetError",
                False,
            ),
        ],
        ids=["503", "429", "404", "timeout", "unreachable", "reset"],
    )
    def test_message_retryability_and_one_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        failure: BaseException,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        class _Opener:
            def open(self, request: Any, timeout: float | None = None) -> Any:
                raise failure

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _Opener())
        with caplog.at_level("DEBUG"):
            resp = GatedSitemapWebClient().get(self._URL)
        assert isinstance(resp, WebClientErrorResponse)
        assert resp.message() == message
        assert resp.retryable() is retryable
        [record] = [r for r in caplog.records if r.name.endswith("fetch_refused")]
        assert "fetch refused" in record.getMessage()
        assert "https://safe.example/…(segments=1)" in record.getMessage()
        assert "SECRET" not in caplog.text


class TestWhoDecidesHowLoudAProbeMissIs:
    """USP knows which fetches are speculative; its ``quiet_404`` decides, not us."""

    _HOME = "https://safe.example/"

    @staticmethod
    def _discover(monkeypatch: pytest.MonkeyPatch, routes: dict[str, bytes]) -> None:
        from quarry.http_url import HttpUrl
        from quarry.ingestion.sitemap_reader import SitemapReader

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _FakeOpener(routes))
        SitemapReader().discover(HttpUrl("https://safe.example/"))

    def test_a_well_known_path_miss_is_below_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            self._discover(monkeypatch, {})
        probes = [r for r in caplog.records if "robots.txt" not in r.getMessage()]
        assert [r for r in probes if r.levelno >= logging.WARNING] == []

    def test_only_the_robots_fetch_is_a_usp_warning_on_an_ordinary_miss(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # USP fetches robots.txt without quiet_404, so it reports that miss itself.
        with caplog.at_level(logging.DEBUG):
            self._discover(monkeypatch, {})
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.name for r in warnings] == ["usp.helpers"]
        assert "robots.txt" in warnings[0].getMessage()

    def test_a_listed_sub_sitemap_that_is_missing_is_a_usp_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        routes = {
            "https://safe.example/robots.txt": (
                b"Sitemap: https://safe.example/listed.xml\n"
            )
        }
        with caplog.at_level(logging.DEBUG):
            self._discover(monkeypatch, routes)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.name for r in warnings] == ["usp.helpers"]
        assert "404 Not Found" in warnings[0].getMessage()

    def test_our_own_layer_logs_a_sitemap_status_at_debug_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            self._discover(monkeypatch, {})
        ours = [r for r in caplog.records if r.name.endswith("fetch_refused")]
        assert ours
        assert {r.levelno for r in ours} == {logging.DEBUG}


class TestAnSsrfBlockIsLoggedByOurLayer:
    def test_a_blocked_entry_is_one_warning_from_the_fetch_layer(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo("10.0.0.9"))
        with caplog.at_level(logging.DEBUG):
            GatedSitemapWebClient().get("https://internal.example/x.xml?token=SECRET")
        ours = [r for r in caplog.records if r.name.endswith("fetch_refused")]
        assert [r.levelno for r in ours] == [logging.WARNING]
        assert "SSRF-blocked URL" in ours[0].getMessage()
        assert "SECRET" not in caplog.text

    def test_a_blocked_final_url_after_a_redirect_is_logged_too(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def resolve(host: str, *a: Any, **k: Any) -> Any:
            return _addrinfo(
                "10.0.0.9" if host == "internal.example" else "93.184.216.34"
            )

        monkeypatch.setattr(_GETADDRINFO, resolve)
        monkeypatch.setattr(
            swc, "GUARDED_OPENER", _LandingOpener("http://internal.example/x")
        )
        with caplog.at_level(logging.DEBUG):
            GatedSitemapWebClient().get("https://safe.example/x.xml")
        ours = [r for r in caplog.records if r.name.endswith("fetch_refused")]
        assert [r.levelno for r in ours] == [logging.WARNING]
        assert "SSRF-blocked final URL" in ours[0].getMessage()


class TestUspsGunzipWarningReachesTheLog:
    def test_a_gz_url_with_a_plain_body_warns_from_usp_helpers(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quarry.http_url import HttpUrl
        from quarry.ingestion.sitemap_reader import SitemapReader

        url = "https://safe.example/s.xml.gz"
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(
            swc,
            "GUARDED_OPENER",
            _FakeOpener({url: _urlset_xml("https://safe.example/a")}),
        )
        with caplog.at_level(logging.DEBUG):
            SitemapReader().read(HttpUrl(url))
        gunzip = [
            r
            for r in caplog.records
            if r.name == "usp.helpers" and "Unable to gunzip" in r.getMessage()
        ]
        assert [r.levelno for r in gunzip] == [logging.WARNING]


class _BuggyOpener(_FakeOpener):
    """Serve the routes, but raise a bug (not a fetch failure) for one URL."""

    def __init__(self, routes: dict[str, bytes], bug_url: str) -> None:
        super().__init__(routes)
        self._bug_url = bug_url

    def open(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResp:
        if request.full_url == self._bug_url:
            msg = "kaboom-text"
            raise ValueError(msg)
        return super().open(request, timeout)


class TestABugInASubSitemapFetchIsNotPartialSuccess:
    def test_the_read_fails_with_an_error_record(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quarry.http_url import HttpUrl
        from quarry.ingestion.sitemap_reader import (
            SitemapReader,
            UnexpectedSitemapError,
        )

        index = "https://safe.example/index.xml"
        good = "https://safe.example/good.xml"
        bad = "https://safe.example/bad.xml"
        routes = {
            index: _index_xml(good, bad),
            good: _urlset_xml("https://safe.example/page"),
        }
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _BuggyOpener(routes, bad))
        with caplog.at_level(logging.DEBUG), pytest.raises(UnexpectedSitemapError):
            SitemapReader().read(HttpUrl(index))
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("unexpected error" in r.getMessage() for r in errors)


class TestTheClientRecordsABugAndReRaises:
    def test_it_records_the_class_logs_frames_without_the_message_and_re_raises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = "https://safe.example/bad.xml?token=SECRET"
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _BuggyOpener({}, url))
        client = GatedSitemapWebClient()
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(ValueError, match="kaboom-text"),
        ):
            client.get(url)
        assert client.take_bug() == "ValueError"
        assert client.take_bug() == ""
        [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert "ValueError" in record.getMessage()
        assert "File " in record.getMessage()
        assert "kaboom-text" not in record.getMessage()
        assert "SECRET" not in record.getMessage()

    def test_a_refusal_is_not_a_bug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _FakeOpener({}))
        client = GatedSitemapWebClient()
        client.get("https://safe.example/missing.xml")
        assert client.take_bug() == ""


class TestDocumentDefectsAreNotBugs:
    @staticmethod
    def _read(
        monkeypatch: pytest.MonkeyPatch, routes: dict[str, bytes], index: str
    ) -> list[str]:
        from quarry.http_url import HttpUrl
        from quarry.ingestion.sitemap_reader import SitemapReader

        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _FakeOpener(routes))
        return [p.url for p in SitemapReader().read(HttpUrl(index))]

    def test_a_self_listing_index_keeps_the_other_pages_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        index = "https://safe.example/index.xml"
        good = "https://safe.example/good.xml"
        routes = {
            index: _index_xml(index, good),
            good: _urlset_xml("https://safe.example/page"),
        }
        with caplog.at_level(logging.DEBUG):
            urls = self._read(monkeypatch, routes, index)
        assert urls == ["https://safe.example/page"]
        quarry = [r for r in caplog.records if r.name.startswith("quarry")]
        assert not [r for r in quarry if r.levelno >= logging.ERROR]

    def test_a_corrupt_gz_sub_sitemap_keeps_the_other_pages_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        index = "https://safe.example/index.xml"
        good = "https://safe.example/good.xml"
        bad = "https://safe.example/bad.xml.gz"
        routes = {
            index: _index_xml(good, bad),
            good: _urlset_xml("https://safe.example/page"),
            bad: b"\x1f\x8b\x08\x00 corrupt, not gzip",
        }
        with caplog.at_level(logging.DEBUG):
            urls = self._read(monkeypatch, routes, index)
        assert urls == ["https://safe.example/page"]
        quarry = [r for r in caplog.records if r.name.startswith("quarry")]
        assert not [r for r in quarry if r.levelno >= logging.ERROR]


class TestDiscoverFollowsTheSameRule:
    _ROBOTS = "https://safe.example/robots.txt"

    @staticmethod
    def _robots(*sitemaps: str) -> bytes:
        return "".join(f"Sitemap: {url}\n" for url in sitemaps).encode()

    def test_a_bug_in_a_listed_sub_sitemap_fails_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.http_url import HttpUrl
        from quarry.ingestion.sitemap_reader import (
            SitemapReader,
            UnexpectedSitemapError,
        )

        bad = "https://safe.example/bad.xml"
        routes = {self._ROBOTS: self._robots(bad)}
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _BuggyOpener(routes, bad))
        with pytest.raises(UnexpectedSitemapError) as e:
            SitemapReader().discover(HttpUrl("https://safe.example/"))
        assert e.value.reported_as == "ValueError"

    def test_a_listed_sub_sitemap_that_is_missing_is_debug(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quarry.http_url import HttpUrl
        from quarry.ingestion.sitemap_reader import SitemapReader

        routes = {self._ROBOTS: self._robots("https://safe.example/missing.xml")}
        monkeypatch.setattr(_GETADDRINFO, _resolver({}))
        monkeypatch.setattr(swc, "GUARDED_OPENER", _FakeOpener(routes))
        with caplog.at_level(logging.DEBUG):
            SitemapReader().discover(HttpUrl("https://safe.example/"))
        nodes = [
            r
            for r in caplog.records
            if r.getMessage().startswith("sitemap node failed")
        ]
        assert nodes
        assert {r.levelno for r in nodes} == {logging.DEBUG}
