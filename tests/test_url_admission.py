"""Tests for quarry.url_admission: the single gate every fetched URL passes."""

from __future__ import annotations

from typing import Any

import pytest

from quarry.url_admission import UrlAdmission
from tests.test_url_syntax import _LEGITIMATE, _MALFORMED

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


class TestAdmission:
    """The gate refuses every non-plain URL before any DNS lookup."""

    @pytest.mark.parametrize("url", _MALFORMED.values(), ids=_MALFORMED.keys())
    def test_a_malformed_url_is_refused_with_fixed_text(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_GETADDRINFO, _boom)
        assert UrlAdmission.reject_reason(url) == "malformed URL"

    @pytest.mark.parametrize("url", _LEGITIMATE.values(), ids=_LEGITIMATE.keys())
    def test_a_legitimate_url_still_passes(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolve_to(monkeypatch, _PUBLIC)
        assert UrlAdmission.reject_reason(url) is None

    def test_an_unsupported_scheme_is_not_echoed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_GETADDRINFO, _boom)
        assert UrlAdmission.reject_reason("SECRET_scheme:foo") == ("unsupported scheme")
        assert UrlAdmission.reject_reason("SECRET_scheme://h/") == (
            "unsupported scheme"
        )
