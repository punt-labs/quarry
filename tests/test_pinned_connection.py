"""Tests for the DNS-rebinding pin: resolve-once, validate, connect-to-pinned-IP.

These prove the TOCTOU window is closed *by construction* — there is one
``getaddrinfo`` on the safety path and the socket connects to a member of that
validated result — and that pinning the TCP target does not touch TLS SNI,
certificate verification, or the ``Host`` header.  Only ``getaddrinfo`` (as seen
by :class:`UrlSafetyCheck`) and ``socket.create_connection`` (as seen by the
pinned connection) are patched; the connection and TLS wiring are real stdlib.

Tier: unit.  No network, no real DNS, no real TLS handshake.
"""

from __future__ import annotations

import ssl
from typing import Any, cast
from urllib.error import URLError

import pytest
from usp.web_client.abstract_client import WebClientErrorResponse

from quarry.ingestion.pinned_connection import (
    PinnedHTTPConnection,
    PinnedHTTPSConnection,
)
from quarry.ingestion.pinned_opener import PinnedHTTPSHandler
from quarry.ingestion.ssrf_redirect import GUARDED_OPENER, SsrfGuardedRedirectHandler
from quarry.ingestion.web_fetch import WebFetcher
from quarry.sitemap_web_client import GatedSitemapWebClient
from quarry.url_safety import UrlRejectedError, UrlSafetyCheck

_GETADDRINFO = "quarry.url_safety.socket_module.getaddrinfo"
_CREATE_CONNECTION = "quarry.ingestion.pinned_connection.socket.create_connection"
_PUBLIC = "93.184.216.34"
_PUBLIC_2 = "93.184.216.35"
_PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
_BLOCKED = "169.254.169.254"  # link-local metadata address
_BLOCKED_RFC1918 = "10.0.0.9"
# A DNS label may be at most 63 chars; 64 makes getaddrinfo raise
# UnicodeError (a ValueError subclass, not OSError) at IDNA-encode time,
# before any network lookup — so these tests are hermetic.
_OVERLONG_LABEL_URL = "http://" + "a" * 64 + ".example/"


def _addrinfo(*ips: str) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    """One getaddrinfo record per *ip* (v6 sockaddr is a 4-tuple, v4 a 2-tuple)."""
    records: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
    for ip in ips:
        family = 10 if ":" in ip else 2
        sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
        records.append((family, 1, 6, "", sockaddr))
    return records


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *ips: str) -> None:
    """Pin every getaddrinfo call to resolve to *ips*."""
    monkeypatch.setattr(_GETADDRINFO, lambda *a, **k: _addrinfo(*ips))


class _Resolver:
    """A getaddrinfo stub returning a different result set on the Nth call.

    Models DNS rebinding: the admission-time ``reject_reason`` resolution sees a
    safe address; the later connect-time resolution sees a blocked one.
    """

    def __init__(self, *rounds: tuple[str, ...]) -> None:
        self._rounds = list(rounds)
        self.calls = 0

    def __call__(self, *_a: Any, **_k: Any) -> Any:
        ips = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        return _addrinfo(*ips)


class _FakeSocket:
    """A stand-in socket recording close(); enough for the stdlib connect body."""

    def __init__(self) -> None:
        self.closed = False

    def setsockopt(self, *_a: Any) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingContext:
    """An SSL context stand-in that records the server_hostname it is asked to use."""

    def __init__(self) -> None:
        self.server_hostname: str | None = None
        self.check_hostname = True

    def wrap_socket(self, sock: Any, *, server_hostname: str | None = None) -> Any:
        self.server_hostname = server_hostname
        return sock


class _RaisingContext:
    """An SSL context stand-in whose handshake fails, to prove socket cleanup."""

    def __init__(self) -> None:
        self.check_hostname = True

    def wrap_socket(self, _sock: Any, *, server_hostname: str | None = None) -> Any:
        raise ssl.SSLError("handshake failed")


def _record_create_connection(
    monkeypatch: pytest.MonkeyPatch, sock: _FakeSocket
) -> list[tuple[str, int]]:
    """Patch create_connection to record each target and return *sock*."""
    targets: list[tuple[str, int]] = []

    def _fake(address: tuple[str, int], *_a: Any, **_k: Any) -> _FakeSocket:
        targets.append(address)
        return sock

    monkeypatch.setattr(_CREATE_CONNECTION, _fake)
    return targets


def _forbid_create_connection(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Patch create_connection to record targets; the test asserts it is unused."""
    targets: list[tuple[str, int]] = []

    def _fake(address: tuple[str, int], *_a: Any, **_k: Any) -> _FakeSocket:
        targets.append(address)
        return _FakeSocket()

    monkeypatch.setattr(_CREATE_CONNECTION, _fake)
    return targets


class TestRebindingSimulation:
    """The headline: a resolution that changes between admission and connect."""

    def test_blocked_at_connect_never_opens_a_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Safe at admission, blocked at connect: fetch rejects, no socket opened.

        ``WebFetcher.fetch`` gates the URL (getaddrinfo #1 -> public), then the
        pinned connection re-resolves inside connect (getaddrinfo #2 -> blocked)
        and raises before any socket is created.  ``create_connection`` is never
        reached with a blocked literal — the window is closed by construction.
        """
        resolver = _Resolver((_PUBLIC,), (_BLOCKED,))
        monkeypatch.setattr(_GETADDRINFO, resolver)
        targets = _forbid_create_connection(monkeypatch)
        with pytest.raises(ValueError, match=r"blocked address|metadata"):
            WebFetcher().fetch("https://rebind.test/page")
        assert targets == []  # no socket ever opened to the blocked address
        assert resolver.calls >= 2  # admission gate, then the connect-time resolve

    def test_pin_targets_the_exact_validated_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The socket connects to the validated IP literal, not a re-resolution.

        getaddrinfo returns one public address; the connection must target that
        exact literal, proving there is no independent second resolution that a
        rebinder could poison.
        """
        _resolve_to(monkeypatch, _PUBLIC)
        sock = _FakeSocket()
        targets = _record_create_connection(monkeypatch, sock)
        conn = PinnedHTTPConnection("pin.test")
        conn.connect()
        assert targets == [(_PUBLIC, 80)]  # pinned to the validated literal
        assert cast("object", conn.sock) is sock


class TestTlsSemantics:
    """Class 4: pinning the address must not touch SNI, trust, or the Host header."""

    def test_sni_and_host_stay_the_hostname_not_the_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wrap_socket is asked for the hostname; the Host header is the hostname."""
        _resolve_to(monkeypatch, _PUBLIC)
        _record_create_connection(monkeypatch, _FakeSocket())
        ctx = _RecordingContext()
        conn = PinnedHTTPSConnection("example.com", context=cast("ssl.SSLContext", ctx))
        conn.connect()
        assert ctx.server_hostname == "example.com"  # SNI/cert bound to hostname
        assert conn.host == "example.com"  # drives the auto Host header, untouched

    def test_ipv6_validated_address_yields_a_valid_literal_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An IPv6 resolution pins to the bracket-free IPv6 literal string."""
        _resolve_to(monkeypatch, _PUBLIC_V6)
        sock = _FakeSocket()
        targets = _record_create_connection(monkeypatch, sock)
        conn = PinnedHTTPSConnection(
            "example.com", context=cast("ssl.SSLContext", _RecordingContext())
        )
        conn.connect()
        assert targets == [(_PUBLIC_V6, 443)]

    def test_public_fetch_context_uses_system_trust_not_a_pinned_ca(self) -> None:
        """The opener's HTTPS context verifies against the system store, hostname on.

        This is the deliberate opposite of the daemon-RPC pinned-CA context: a
        single-CA context would carry exactly one cert, so >1 CA proves the
        system trust store is in use.
        """
        opener = SsrfGuardedRedirectHandler.build_opener()
        https = next(
            h for h in cast("Any", opener).handlers if isinstance(h, PinnedHTTPSHandler)
        )
        ctx = https._context
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert len(ctx.get_ca_certs()) > 1


class TestSocketHygiene:
    """Class 1: multi-record fallback, all-fail surfacing, and no leaked fd."""

    def test_second_validated_address_used_when_first_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused first address falls through to the second — both validated."""
        _resolve_to(monkeypatch, _PUBLIC, _PUBLIC_2)
        good = _FakeSocket()
        attempts: list[tuple[str, int]] = []

        def _fake(address: tuple[str, int], *_a: Any, **_k: Any) -> _FakeSocket:
            attempts.append(address)
            if address[0] == _PUBLIC:
                raise OSError("connection refused")
            return good

        monkeypatch.setattr(_CREATE_CONNECTION, _fake)
        conn = PinnedHTTPConnection("multi.test")
        result = conn._pinned_create_connection(("multi.test", 80), 30, None)
        assert attempts == [(_PUBLIC, 80), (_PUBLIC_2, 80)]
        assert cast("object", result) is good

    def test_all_addresses_refused_surfaces_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every validated address refuses, the last OSError surfaces."""
        _resolve_to(monkeypatch, _PUBLIC, _PUBLIC_2)

        def _fake(address: tuple[str, int], *_a: Any, **_k: Any) -> _FakeSocket:
            raise OSError(f"refused {address[0]}")

        monkeypatch.setattr(_CREATE_CONNECTION, _fake)
        conn = PinnedHTTPConnection("multi.test")
        with pytest.raises(OSError, match=f"refused {_PUBLIC_2}"):
            conn._pinned_create_connection(("multi.test", 80), 30, None)

    def test_wrap_socket_failure_closes_underlying_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TLS handshake failure closes the pinned socket (do_open cleanup)."""
        _resolve_to(monkeypatch, _PUBLIC)
        sock = _FakeSocket()
        _record_create_connection(monkeypatch, sock)
        opener = SsrfGuardedRedirectHandler.build_opener()
        https = next(
            h for h in cast("Any", opener).handlers if isinstance(h, PinnedHTTPSHandler)
        )
        monkeypatch.setattr(https, "_context", _RaisingContext())
        with pytest.raises(OSError):  # SSLError -> URLError, both OSError subclasses
            opener.open("https://example.com/x", timeout=5)
        assert sock.closed is True  # h.close() in do_open freed the fd


class TestRemoteLocalDivergence:
    """Class 3: both attacker-reachable fetch paths refuse a rebind identically."""

    def test_webfetcher_and_sitemap_client_both_refuse_connect_rebind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Safe-at-admission, blocked-at-connect is refused via both surfaces."""
        forbidden = _forbid_create_connection(monkeypatch)

        monkeypatch.setattr(_GETADDRINFO, _Resolver((_PUBLIC,), (_BLOCKED_RFC1918,)))
        with pytest.raises(ValueError):
            WebFetcher().fetch("https://rebind.test/page")

        monkeypatch.setattr(_GETADDRINFO, _Resolver((_PUBLIC,), (_BLOCKED_RFC1918,)))
        response = GatedSitemapWebClient().get("https://rebind.test/sitemap.xml")
        assert isinstance(response, WebClientErrorResponse)
        assert response.retryable() is False
        assert forbidden == []  # neither surface opened a socket to the blocked IP


class TestExceptionBoundaries:
    """Class 2: the resolver raises UrlRejectedError; reject_reason never raises."""

    def test_validated_addresses_raises_urlrejected_on_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolve_to(monkeypatch, _BLOCKED_RFC1918)
        with pytest.raises(UrlRejectedError):
            UrlSafetyCheck.validated_addresses("target.test")

    def test_validated_addresses_raises_urlrejected_not_oserror_on_dns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: Any, **_k: Any) -> Any:
            raise OSError("Name or service not known")

        monkeypatch.setattr(_GETADDRINFO, _raise)
        with pytest.raises(UrlRejectedError) as exc_info:
            UrlSafetyCheck.validated_addresses("nope.test")
        assert isinstance(exc_info.value, ValueError)  # not a bare OSError
        assert not isinstance(exc_info.value, OSError)

    def test_validated_addresses_raises_urlrejected_on_overlong_idna_label(
        self,
    ) -> None:
        """An over-long IDNA label (getaddrinfo UnicodeError) fails closed."""
        with pytest.raises(UrlRejectedError) as exc_info:
            UrlSafetyCheck.validated_addresses("a" * 64 + ".example")
        assert not isinstance(exc_info.value, OSError)  # UnicodeError, wrapped

    def test_reject_reason_returns_string_on_overlong_idna_label(self) -> None:
        """reject_reason never raises on an over-long label — it returns a reason."""
        reason = UrlSafetyCheck.reject_reason(_OVERLONG_LABEL_URL)
        assert reason is not None
        assert "cannot resolve hostname" in reason

    def test_sitemap_client_returns_nonretryable_on_overlong_idna_label(self) -> None:
        """GatedSitemapWebClient.get does not raise: it reports a skip response."""
        response = GatedSitemapWebClient().get(_OVERLONG_LABEL_URL)
        assert isinstance(response, WebClientErrorResponse)
        assert response.retryable() is False

    def test_webfetcher_surfaces_overlong_idna_label_as_rejection(self) -> None:
        """WebFetcher.fetch surfaces it as a clean ValueError URL rejection."""
        with pytest.raises(ValueError, match="URL rejected"):
            WebFetcher().fetch(_OVERLONG_LABEL_URL)

    def test_reject_reason_returns_string_on_malformed_url(self) -> None:
        """A malformed URL yields a reason string, not a crash (never raises)."""
        reason = UrlSafetyCheck.reject_reason("http://[")
        assert reason is not None
        assert "malformed URL" in reason


class TestSchemeLockdown:
    """The shared opener serves http(s) ONLY — non-http(s) schemes cannot open.

    Defense in depth behind each caller's scheme pre-check: the opener carries
    no FTP/File/Data handler, so an ``ftp://``/``file://``/``data:`` URL hits
    ``UnknownHandler`` and raises ``URLError`` — it is never served, even if a
    future caller forgets to gate the scheme itself.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://internal.host/secret",
            "data:text/html,<h1>hi</h1>",
        ],
    )
    def test_non_http_scheme_is_refused_by_the_opener(self, url: str) -> None:
        with pytest.raises(URLError):
            GUARDED_OPENER.open(url, timeout=5)

    def test_opener_carries_no_non_http_scheme_handler(self) -> None:
        """No FTP/File/Data handler is installed; the pinned + redirect set is."""
        names = {type(h).__name__ for h in cast("Any", GUARDED_OPENER).handlers}
        assert names.isdisjoint({"FTPHandler", "FileHandler", "DataHandler"})
        assert {"PinnedHTTPHandler", "PinnedHTTPSHandler"} <= names
        assert "SsrfGuardedRedirectHandler" in names
