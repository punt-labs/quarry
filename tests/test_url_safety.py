"""Unit tests for quarry.url_safety.UrlSafetyCheck — the SSRF classifier itself.

These exercise ``reject_reason`` directly, not through a fetch, so every blocked
address class and every fail-closed branch is proven in isolation.  Only
``getaddrinfo`` is mocked (to pin a specific resolution); the classification is
the real ``UrlSafetyCheck`` logic.
"""

from __future__ import annotations

from typing import Any

import pytest

from quarry.url_safety import UrlSafetyCheck

_GETADDRINFO = "quarry.url_safety.socket_module.getaddrinfo"
_PUBLIC = "93.184.216.34"


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


def _boom(*_a: Any, **_k: Any) -> Any:
    """A resolver that must never be called (hostname-only rejects skip DNS)."""
    raise AssertionError("getaddrinfo must not be called on this path")


class TestSafeUrl:
    """A well-formed public URL passes the gate."""

    def test_public_url_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _resolve_to(monkeypatch, _PUBLIC)
        assert UrlSafetyCheck.reject_reason("https://example.com/page") is None

    def test_public_ipv6_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _resolve_to(monkeypatch, "2606:2800:220:1:248:1893:25c8:1946")
        assert UrlSafetyCheck.reject_reason("https://example.com/") is None


class TestSchemeAndHost:
    """Rejects that need no DNS resolution."""

    @pytest.mark.parametrize(
        "url", ["ftp://example.com", "file:///etc/passwd", "gopher://x"]
    )
    def test_non_http_scheme_rejected(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_GETADDRINFO, _boom)
        reason = UrlSafetyCheck.reject_reason(url)
        assert reason is not None
        assert "unsupported scheme" in reason

    def test_missing_host_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_GETADDRINFO, _boom)
        reason = UrlSafetyCheck.reject_reason("https:///just/a/path")
        assert reason is not None
        assert "missing hostname" in reason

    @pytest.mark.parametrize(
        "host",
        [
            "169.254.169.254",
            "metadata.google.internal",
            "metadata",
            "instance-data.ec2.internal",
        ],
    )
    def test_metadata_hostname_rejected_without_dns(
        self, host: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud-metadata hosts are blocked by name, before any resolution."""
        monkeypatch.setattr(_GETADDRINFO, _boom)
        reason = UrlSafetyCheck.reject_reason(f"http://{host}/latest/meta-data/")
        assert reason is not None
        assert "metadata hostname" in reason

    def test_dot_local_hostname_rejected_without_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_GETADDRINFO, _boom)
        reason = UrlSafetyCheck.reject_reason("http://myprinter.local/status")
        assert reason is not None
        assert ".local" in reason


class TestBlockedResolvedAddress:
    """Every blocked address class is rejected once a host resolves to it."""

    @pytest.mark.parametrize(
        ("label", "resolved"),
        [
            ("link-local", "169.254.1.1"),
            ("loopback-v4", "127.0.0.1"),
            ("loopback-v6", "::1"),
            ("rfc1918-10", "10.0.0.5"),
            ("rfc1918-172", "172.16.3.4"),
            ("rfc1918-192", "192.168.1.10"),
            ("unspecified-v6", "::"),
            ("mapped-loopback", "::ffff:127.0.0.1"),
            ("mapped-link-local", "::ffff:169.254.1.1"),
            # Unmaps to IPv4 0.0.0.0 — exercises the v4 is_unspecified branch.
            ("mapped-unspecified-v4", "::ffff:0.0.0.0"),
            ("nat64-wellknown", "64:ff9b::1"),
            ("nat64-embeds-loopback", "64:ff9b::7f00:1"),
        ],
    )
    def test_blocked_class_rejected(
        self, label: str, resolved: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolve_to(monkeypatch, resolved)
        reason = UrlSafetyCheck.reject_reason("https://target.test/x")
        assert reason is not None
        assert "blocked address" in reason

    @pytest.mark.parametrize(
        ("label", "resolved"),
        [
            ("cgnat", "100.64.1.1"),
            ("mapped-cgnat", "::ffff:100.64.1.1"),
        ],
    )
    def test_cgnat_class_rejected(
        self, label: str, resolved: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CGNAT (RFC 6598) has its own reason wording; mapped form unmaps to it."""
        _resolve_to(monkeypatch, resolved)
        reason = UrlSafetyCheck.reject_reason("https://target.test/x")
        assert reason is not None
        assert "CGNAT address" in reason

    def test_multi_record_dns_rejects_if_any_internal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host resolving to [public, private] is rejected — every record checked."""
        monkeypatch.setattr(
            _GETADDRINFO, lambda *a, **k: _addrinfo(_PUBLIC, "10.0.0.9")
        )
        reason = UrlSafetyCheck.reject_reason("https://mixed.test/x")
        assert reason is not None
        assert "blocked address 10.0.0.9" in reason


class TestFailClosed:
    """A resolution or parse failure returns a reason, never an exception."""

    def test_getaddrinfo_failure_returns_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: Any, **_k: Any) -> Any:
            raise OSError("Name or service not known")

        monkeypatch.setattr(_GETADDRINFO, _raise)
        reason = UrlSafetyCheck.reject_reason("https://cannot-resolve.test/x")
        assert reason is not None
        assert "cannot resolve hostname" in reason

    def test_unparseable_resolved_address_returns_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolver returning a bogus address string is rejected, not crashed."""
        monkeypatch.setattr(
            _GETADDRINFO, lambda *a, **k: [(2, 1, 6, "", ("not-an-ip", 0))]
        )
        reason = UrlSafetyCheck.reject_reason("https://bogus.test/x")
        assert reason is not None
        assert "cannot parse resolved address" in reason
