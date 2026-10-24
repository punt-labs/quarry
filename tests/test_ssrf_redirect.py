"""Unit tests for SsrfGuardedRedirectHandler and the shared GUARDED_OPENER.

These drive the redirect handler directly and through a fake opener harness (no
network): a redirect to a blocked address must raise before the target is ever
opened, a safe hop must be followed for every 30x code, and the single
module-level opener must be the same object both fetch surfaces share.
"""

from __future__ import annotations

import io
import urllib.request
from http.client import HTTPMessage
from typing import Any

import pytest

import quarry.ingestion.opened_response as wf
import quarry.ingestion.ssrf_redirect as sr
import quarry.sitemap_web_client as swc
from quarry.ingestion.ssrf_redirect import (
    GUARDED_OPENER,
    RedirectRejectedError,
    SsrfGuardedRedirectHandler,
)

_GETADDRINFO = "quarry.url_safety.socket_module.getaddrinfo"
_PUBLIC = "93.184.216.34"

# Every HTTP redirect status urllib's base handler follows.
_REDIRECT_CODES = [301, 302, 303, 307, 308]

_BLOCKED_CLASSES = [
    ("link-local", "169.254.1.1"),
    ("loopback-v4", "127.0.0.1"),
    ("loopback-v6", "::1"),
    ("rfc1918", "10.0.0.7"),
    ("rfc1918-192", "192.168.1.5"),
    ("cgnat", "100.64.2.3"),
    ("mapped-loopback", "::ffff:127.0.0.1"),
    ("mapped-link-local", "::ffff:169.254.1.1"),
    ("nat64", "64:ff9b::7f00:1"),
]


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    """One getaddrinfo record for *ip* (v6 sockaddr is a 4-tuple, v4 a 2-tuple)."""
    family = 10 if ":" in ip else 2
    sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, 1, 6, "", sockaddr)]


def _resolve(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(ip))


class TestRedirectRequest:
    """The per-hop gate on redirect_request itself."""

    @pytest.mark.parametrize("code", _REDIRECT_CODES)
    def test_safe_redirect_followed_for_every_code(
        self, code: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A public-resolving target returns a Request for each 30x status."""
        _resolve(monkeypatch, _PUBLIC)
        handler = SsrfGuardedRedirectHandler()
        req = urllib.request.Request("https://public.example/start")
        result = handler.redirect_request(
            req,
            io.BytesIO(b""),
            code,
            "Redirect",
            HTTPMessage(),
            "https://public.example/next",
        )
        assert result is not None
        assert result.full_url == "https://public.example/next"

    @pytest.mark.parametrize("code", _REDIRECT_CODES)
    @pytest.mark.parametrize(("label", "resolved"), _BLOCKED_CLASSES)
    def test_blocked_redirect_raises_before_open_for_every_code(
        self,
        code: int,
        label: str,
        resolved: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A target resolving to any blocked class raises, for each 30x status.

        Raising (not returning ``None``) is what aborts the fetch: the opener
        never opens the target, so no connection to the internal address occurs.
        """
        _resolve(monkeypatch, resolved)
        handler = SsrfGuardedRedirectHandler()
        req = urllib.request.Request("https://public.example/start")
        with pytest.raises(RedirectRejectedError, match="redirect target rejected"):
            handler.redirect_request(
                req,
                io.BytesIO(b""),
                code,
                "Redirect",
                HTTPMessage(),
                "http://target.internal/x",
            )

    def test_metadata_ip_redirect_raises_without_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A literal metadata IP is rejected by hostname, before any resolution."""

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("no DNS for a literal metadata IP")

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


class TestSharedOpener:
    """GUARDED_OPENER is the single opener both fetch surfaces share."""

    def test_singleton_identity_across_surfaces(self) -> None:
        """WebFetcher and the sitemap client bind the same opener object.

        The module ``__dict__`` is read directly because the name is imported,
        not re-exported — an attribute access would trip pyright's implicit-reexport
        check, yet identity of the shared object is exactly what must be proven.
        """
        assert wf.__dict__["GUARDED_OPENER"] is GUARDED_OPENER
        assert swc.__dict__["GUARDED_OPENER"] is GUARDED_OPENER
        assert sr.GUARDED_OPENER is GUARDED_OPENER

    def test_build_opener_returns_opener_director(self) -> None:
        """build_opener yields an OpenerDirector; its gating is proven below.

        The classmethod runs at import to build the shared singleton, so its
        redirect gating is exercised by TestRedirectRequest and
        TestOpenerNeverOpensInternal; here we only pin the return contract.
        """
        opener = SsrfGuardedRedirectHandler.build_opener()
        assert isinstance(opener, urllib.request.OpenerDirector)


class TestOpenerNeverOpensInternal:
    """End-to-end through a fake opener: the internal target is never opened."""

    def test_internal_redirect_target_never_opened(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 302 to an internal host aborts; only the start URL is ever opened."""
        start = "https://public.example/start"
        internal = "http://internal.service/secret"
        _resolve(monkeypatch, "10.0.0.9")
        recorder = _RecordingHandler(start, internal)
        opener = urllib.request.OpenerDirector()
        opener.add_handler(SsrfGuardedRedirectHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(recorder)
        with pytest.raises(RedirectRejectedError, match="redirect target rejected"):
            opener.open(start)
        assert recorder.opened == [start]  # internal target never fetched
        assert recorder.redirect is not None
        assert recorder.redirect.closed  # the refused hop's response is released

    def test_safe_redirect_chain_is_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A public→public 302 chain opens both hops through the gate."""
        start = "https://public.example/start"
        landing = "https://public.example/landing"
        _resolve(monkeypatch, _PUBLIC)
        recorder = _RecordingHandler(start, landing)
        opener = urllib.request.OpenerDirector()
        opener.add_handler(SsrfGuardedRedirectHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(recorder)
        with opener.open(start) as resp:
            assert resp.url == landing
        assert recorder.opened == [start, landing]


class _FakeResp:
    """Minimal response object for a fake urllib handler."""

    def __init__(self, code: int, headers: HTTPMessage, url: str) -> None:
        self.code = code
        self.msg = "Found" if code == 302 else "OK"
        self._headers = headers
        self.url = url
        self.closed = False

    def info(self) -> HTTPMessage:
        return self._headers

    def read(self, *_a: Any) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


class _RecordingHandler(urllib.request.BaseHandler):
    """Intercept opens: 302 the start URL to *target*, record every URL opened."""

    def __init__(self, start: str, target: str, code: int = 302) -> None:
        self._start = start
        self._target = target
        self._code = code
        self.opened: list[str] = []
        self.redirect: _FakeResp | None = None

    def _open(self, req: urllib.request.Request) -> _FakeResp:
        url = req.full_url
        self.opened.append(url)
        headers = HTTPMessage()
        if url == self._start:
            headers["Location"] = self._target
            self.redirect = _FakeResp(self._code, headers, url)
            return self.redirect
        headers["Content-Type"] = "text/html"
        return _FakeResp(200, headers, url)

    def http_open(self, req: urllib.request.Request) -> _FakeResp:
        return self._open(req)

    def https_open(self, req: urllib.request.Request) -> _FakeResp:
        return self._open(req)


_MALFORMED = ["http://[", "http://[::1", "//[x/", "http://\u2100.com"]


def _guarded_opener(
    recorder: urllib.request.BaseHandler,
) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    opener.add_handler(SsrfGuardedRedirectHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(recorder)
    return opener


class TestAMalformedLocationIsARefusal:
    @pytest.mark.parametrize("location", _MALFORMED)
    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_the_hop_is_refused_before_anything_else_is_opened(
        self, location: str, code: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = "https://public.example/start"
        _resolve(monkeypatch, _PUBLIC)
        recorder = _RecordingHandler(start, location, code)
        with pytest.raises(RedirectRejectedError, match="malformed redirect target"):
            _guarded_opener(recorder).open(start)
        assert recorder.opened == [start]
        assert recorder.redirect is not None
        assert recorder.redirect.closed

    @pytest.mark.parametrize("location", _MALFORMED)
    def test_the_sitemap_path_answers_with_an_error_response_and_no_bug(
        self, location: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from usp.web_client.abstract_client import WebClientErrorResponse

        import quarry.sitemap_web_client as swc

        start = "https://public.example/start"
        _resolve(monkeypatch, _PUBLIC)
        monkeypatch.setattr(
            swc, "GUARDED_OPENER", _guarded_opener(_RecordingHandler(start, location))
        )
        client = swc.GatedSitemapWebClient()
        response = client.get(start)
        assert isinstance(response, WebClientErrorResponse)
        assert response.message() == "malformed redirect target"
        assert client.take_bug() == ""

    @pytest.mark.parametrize("location", _MALFORMED)
    def test_the_ingest_path_raises_a_refusal(
        self, location: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import quarry.ingestion.opened_response as opened
        from quarry.ingestion.fetch_refused import FetchRefusedError

        start = "https://public.example/start"
        _resolve(monkeypatch, _PUBLIC)
        monkeypatch.setattr(
            opened,
            "GUARDED_OPENER",
            _guarded_opener(_RecordingHandler(start, location)),
        )
        request = urllib.request.Request(start)
        with pytest.raises(FetchRefusedError, match="malformed redirect target"):
            opened.OpenedResponse.open(request, timeout=5, deadline=1e12)
