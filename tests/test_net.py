"""Tests for LoopbackPolicy — the shared daemon/client host classifier.

Both failure directions must be closed: a bind-all or unknown host must be
treated as remote (require a key), and the loopback set must include the forms
the old ``127.0.0.1``-literal check missed (``localhost``, ``::1``, other
``127.0.0.0/8`` addresses, and IPv4-mapped IPv6 loopback).
"""

from __future__ import annotations

import pytest

from quarry.net import LoopbackPolicy


class TestIsLoopback:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.5",  # all of 127.0.0.0/8 is loopback
            "127.255.255.254",
            "::1",
            "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
            "localhost",
            "Localhost",  # hostnames are case-insensitive (RFC 4343)
            "LOCALHOST",
        ],
    )
    def test_loopback_hosts(self, host: str) -> None:
        assert LoopbackPolicy(host).is_loopback is True

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",  # noqa: S104 — bind-all is remote-reachable
            "::",  # IPv6 bind-all
            "10.0.0.1",
            "192.168.1.5",
            "8.8.8.8",
            "example.com",  # unresolved name — fail closed to remote
            "not-an-ip",
            "",
        ],
    )
    def test_non_loopback_hosts(self, host: str) -> None:
        assert LoopbackPolicy(host).is_loopback is False


class TestEnforceBindKey:
    def test_non_loopback_without_key_refuses(self) -> None:
        with pytest.raises(SystemExit, match="Refusing to bind"):
            LoopbackPolicy("0.0.0.0").enforce_bind_key(None)  # noqa: S104

    def test_non_loopback_with_key_allowed(self) -> None:
        LoopbackPolicy("0.0.0.0").enforce_bind_key("a-secret-key")  # noqa: S104

    def test_loopback_without_key_allowed(self) -> None:
        # Loopback binds get an auto-generated serve.token, so no key needed.
        LoopbackPolicy("127.0.0.1").enforce_bind_key(None)

    def test_localhost_without_key_allowed(self) -> None:
        # The bug this fixes: localhost is loopback and must not demand a key.
        LoopbackPolicy("localhost").enforce_bind_key(None)

    def test_ipv6_loopback_without_key_allowed(self) -> None:
        LoopbackPolicy("::1").enforce_bind_key(None)

    def test_unknown_name_requires_key(self) -> None:
        # Fail closed: an unresolved name is treated as remote.
        with pytest.raises(SystemExit, match="Refusing to bind"):
            LoopbackPolicy("some-host.internal").enforce_bind_key(None)
