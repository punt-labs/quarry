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
            "localhost.",  # FQDN root label — the same host
            " localhost ",  # surrounding whitespace is never significant
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


class TestIsLiteralLoopback:
    """Token-presentation gate: literal loopback IP only, never a name."""

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "127.0.0.5", "127.255.255.254", "::1", "::ffff:127.0.0.1"],
    )
    def test_literal_loopback_ips(self, host: str) -> None:
        assert LoopbackPolicy(host).is_literal_loopback is True

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",  # a NAME is never a literal loopback (resolver-controlled)
            "Localhost",
            "localhost.",
            " localhost ",
            "0.0.0.0",  # noqa: S104 — bind-all is remote-reachable
            "::",
            "10.0.0.1",
            "example.com",
            "",
        ],
    )
    def test_names_and_remote_are_not_literal_loopback(self, host: str) -> None:
        assert LoopbackPolicy(host).is_literal_loopback is False

    def test_localhost_is_bind_loopback_but_not_literal_loopback(self) -> None:
        # The split: the NAME classifies loopback for the bind gate, but is NOT
        # a literal-loopback token-presentation target.
        policy = LoopbackPolicy("localhost")
        assert policy.is_loopback is True
        assert policy.is_literal_loopback is False


class TestCanonicalHost:
    @pytest.mark.parametrize(
        "name", ["localhost", "Localhost", "LOCALHOST", "localhost.", " localhost "]
    )
    def test_loopback_name_canonicalizes_to_ipv4_literal(self, name: str) -> None:
        assert LoopbackPolicy(name).canonical_host == "127.0.0.1"

    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "::1", "gpu.example.com", "10.0.0.5"]
    )
    def test_literal_or_remote_host_unchanged(self, host: str) -> None:
        # Already-normalized hosts pass through unchanged.
        assert LoopbackPolicy(host).canonical_host == host

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" 127.0.0.1 ", "127.0.0.1"),  # a literal loopback, whitespace stripped
            (" gpu.example.com ", "gpu.example.com"),  # remote host, stripped
            ("GPU.example.com", "gpu.example.com"),  # RFC 4343 case-insensitivity
            ("gpu.example.com.", "gpu.example.com"),  # FQDN root label removed
            (" 10.0.0.5 ", "10.0.0.5"),  # remote IP, stripped
        ],
    )
    def test_non_loopback_host_returned_normalized_not_raw(
        self, raw: str, expected: str
    ) -> None:
        # Returning the raw host would let ``login " 127.0.0.1 "`` store an
        # invalid ``wss:// 127.0.0.1 :port`` URL — surrounding whitespace is
        # insignificant everywhere else, so canonical_host must strip it too.
        assert LoopbackPolicy(raw).canonical_host == expected


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


class TestBracketedIPv6:
    """A URL wraps an IPv6 literal as ``[::1]`` (RFC 3986); LoopbackPolicy must
    strip the brackets so ipaddress parses it and both gates classify it."""

    @pytest.mark.parametrize(
        "host", ["[::1]", "[::ffff:127.0.0.1]", "[0:0:0:0:0:0:0:1]"]
    )
    def test_bracketed_loopback_classifies_as_loopback(self, host: str) -> None:
        policy = LoopbackPolicy(host)
        assert policy.is_loopback is True
        assert policy.is_literal_loopback is True

    def test_bracketed_remote_ipv6_is_not_loopback(self) -> None:
        policy = LoopbackPolicy("[2001:db8::5]")
        assert policy.is_loopback is False
        assert policy.is_literal_loopback is False

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("[::1]", "::1"),  # brackets stripped for the policy mapping
            ("[::ffff:127.0.0.1]", "::ffff:127.0.0.1"),
            (" [::1] ", "::1"),  # whitespace + brackets both stripped
        ],
    )
    def test_bracketed_host_canonicalizes_de_bracketed(
        self, host: str, expected: str
    ) -> None:
        assert LoopbackPolicy(host).canonical_host == expected

    def test_bracketed_loopback_bind_not_key_gated(self) -> None:
        # A loopback IPv6 bind (host arrives bracketed from a URL) must not be
        # forced to carry an operator key — the launcher mints its token.
        LoopbackPolicy("[::1]").enforce_bind_key(None)
