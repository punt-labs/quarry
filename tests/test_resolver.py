"""Tier-selection and trust tests for :class:`TargetResolver`.

The trust assertions here are the ones djb hard-gates: the live ``serve.token``
is read ONLY for tier 3 and presented ONLY to the literal loopback IP (never a
name, never a non-loopback target), and the loopback scheme is ``wss`` + pinned
CA when the daemon CA exists, ``ws`` only for a bare plaintext daemon.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.client import ClientConfig, TargetResolver
from quarry.client.config import ClientConfigError
from quarry.client.errors import QuarryConnectionError


def _run_dir(port: int) -> MagicMock:
    run_dir = MagicMock()
    run_dir.port_file.read.return_value = port
    return run_dir


def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUARRY_URL", raising=False)
    monkeypatch.delenv("QUARRY_TOKEN", raising=False)
    monkeypatch.delenv("QUARRY_CA_CERT", raising=False)


class TestTier1Env:
    def test_env_url_and_token_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUARRY_URL", "wss://remote.example:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "env-token")
        cfg = TargetResolver.resolve()
        assert cfg.url == "wss://remote.example:9000"
        bearer = cfg.token
        assert bearer == "env-token"

    def test_env_token_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Loopback host: plaintext + token is same-machine and allowed, so this
        # exercises stripping without tripping the cleartext guard.
        monkeypatch.setenv("QUARRY_URL", "ws://127.0.0.1:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "  tok\n")
        bearer = TargetResolver.resolve().token
        assert bearer == "tok"

    def test_env_whitespace_token_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUARRY_URL", "ws://remote.example:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "   ")
        assert TargetResolver.resolve().token is None

    def test_env_does_not_read_serve_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Trust: the live serve.token is a tier-3-only credential; an env target
        # uses its own QUARRY_TOKEN, never the loopback token.
        monkeypatch.setenv("QUARRY_URL", "wss://remote.example:9000")
        with patch.object(ClientConfig, "loopback_token") as token:
            TargetResolver.resolve()
        token.assert_not_called()

    def test_env_takes_precedence_over_stored_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUARRY_URL", "ws://env.example:9000")
        login = {"quarry": {"url": "wss://stored.example:8420"}}
        with patch("quarry.client.resolver.read_proxy_config", return_value=login):
            assert TargetResolver.resolve().url == "ws://env.example:9000"


class TestTier1EnvSecurity:
    """The MED djb finding: never transmit a bearer in cleartext to a remote."""

    def test_cleartext_token_to_remote_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        monkeypatch.setenv("QUARRY_URL", "ws://remote.example:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "supersecret")
        with pytest.raises(ClientConfigError) as info:
            TargetResolver.resolve()
        # The token is refused (never built into a config, so never transmitted)
        # and its value is not leaked in the error message.
        assert "supersecret" not in str(info.value)

    def test_loopback_plaintext_with_token_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same-machine plaintext is fine, like tier 3.
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        monkeypatch.setenv("QUARRY_URL", "ws://127.0.0.1:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "tok")
        cfg = TargetResolver.resolve()
        bearer = cfg.token
        assert bearer == "tok"
        assert cfg.ca_cert is None

    def test_wss_remote_with_ca_cert_pins_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The sanctioned secure remote-env path: wss + QUARRY_CA_CERT + token.
        ca = tmp_path / "remote-ca.crt"
        ca.write_text("x")
        monkeypatch.setenv("QUARRY_URL", "wss://remote.example:9000")
        monkeypatch.setenv("QUARRY_CA_CERT", str(ca))
        monkeypatch.setenv("QUARRY_TOKEN", "tok")
        cfg = TargetResolver.resolve()
        assert cfg.ca_cert == str(ca)
        bearer = cfg.token
        assert bearer == "tok"

    def test_wss_remote_without_ca_cert_never_pins_local_ca(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A remote wss with no QUARRY_CA_CERT must NOT silently pin the LOCAL
        # daemon CA (wrong CA for a remote host); it fails closed with no pin.
        local_ca = tmp_path / "ca.crt"
        local_ca.write_text("x")
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        monkeypatch.setenv("QUARRY_URL", "wss://remote.example:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "tok")
        with patch("quarry.client.resolver._DAEMON_CA_PATH", local_ca):
            cfg = TargetResolver.resolve()
        assert cfg.ca_cert is None


class TestTier2StoredLogin:
    def test_stored_login_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_env(monkeypatch)
        login = {
            "quarry": {
                "url": "wss://stored.example:8420",
                "ca_cert": "/tmp/ca.crt",
                "headers": {"Authorization": "Bearer stored-tok"},
            }
        }
        with patch("quarry.client.resolver.read_proxy_config", return_value=login):
            cfg = TargetResolver.resolve()
        assert cfg.url == "wss://stored.example:8420"
        bearer = cfg.token
        assert bearer == "stored-tok"

    def test_malformed_toml_falls_through_to_loopback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Class 2: a malformed remote config must not crash — it falls through
        # to the loopback default rather than propagating ValueError.
        _no_env(monkeypatch)
        ca = tmp_path / "ca.crt"
        ca.write_text("x")
        with (
            patch(
                "quarry.client.resolver.read_proxy_config",
                side_effect=ValueError("bad toml"),
            ),
            patch("quarry.client.resolver._DAEMON_CA_PATH", ca),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value="live"),
        ):
            cfg = TargetResolver.resolve()
        assert cfg.url == "wss://127.0.0.1:8420"

    def test_loopback_login_daemon_down_raises_connection_error_with_nudge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The LOW djb finding: a stored loopback login whose quarryd is down must
        # surface the same typed QuarryConnectionError + autostart nudge as
        # tier 3, not a raw ClientConfigError with no guidance.
        _no_env(monkeypatch)
        login = {"quarry": {"url": "wss://127.0.0.1:8420"}}
        with (
            patch("quarry.client.resolver.read_proxy_config", return_value=login),
            patch.object(
                ClientConfig,
                "_serve_token",
                side_effect=ClientConfigError("serve.token unreadable"),
            ),
            pytest.raises(QuarryConnectionError) as info,
        ):
            TargetResolver.resolve()
        assert "quarryd is not running" in info.value.message
        assert info.value.target == "127.0.0.1"


class TestTier3Loopback:
    def test_literal_ip_and_live_token_with_pinned_ca(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _no_env(monkeypatch)
        ca = tmp_path / "ca.crt"
        ca.write_text("x")
        with (
            patch("quarry.client.resolver._DAEMON_CA_PATH", ca),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value="live") as token,
        ):
            cfg = TargetResolver.resolve()
        assert cfg.url == "wss://127.0.0.1:8420"
        assert cfg.ca_cert == str(ca)
        bearer = cfg.token
        assert bearer == "live"
        # Trust: the live token was requested ONLY for the literal loopback IP.
        token.assert_called_once_with("127.0.0.1")

    def test_plaintext_when_no_ca_on_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _no_env(monkeypatch)
        missing_ca = tmp_path / "absent.crt"
        with (
            patch("quarry.client.resolver._DAEMON_CA_PATH", missing_ca),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8500)),
            patch.object(ClientConfig, "loopback_token", return_value="live"),
        ):
            cfg = TargetResolver.resolve()
        assert cfg.url == "ws://127.0.0.1:8500"
        assert cfg.ca_cert is None

    def test_no_serve_port_fails_closed_with_nudge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_env(monkeypatch)
        down = _run_dir(0)
        down.port_file.read.side_effect = FileNotFoundError("no serve.port")
        with (
            patch.object(ClientConfig, "active_run_dir", return_value=down),
            pytest.raises(QuarryConnectionError) as info,
        ):
            TargetResolver.resolve()
        assert "quarryd is not running" in info.value.message
        assert info.value.target == "127.0.0.1"

    def test_missing_loopback_token_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Daemon up (port present) but token unreadable -> fail closed, never send
        # an empty bearer.
        _no_env(monkeypatch)
        with (
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value=None),
            pytest.raises(QuarryConnectionError),
        ):
            TargetResolver.resolve()


class TestTrustBoundary:
    def test_loopback_host_is_literal_never_a_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The serve.token is never presented to a name a resolver could redirect.
        _no_env(monkeypatch)
        with (
            patch("quarry.client.resolver._DAEMON_CA_PATH", tmp_path / "absent.crt"),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value="live"),
        ):
            cfg = TargetResolver.resolve()
        assert "localhost" not in cfg.url
        assert "127.0.0.1" in cfg.url

    def test_stored_login_does_not_read_serve_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Trust: a remote login uses its stored bearer; the loopback serve.token
        # is never read for a non-loopback target.
        _no_env(monkeypatch)
        login = {
            "quarry": {
                "url": "wss://stored.example:8420",
                "headers": {"Authorization": "Bearer stored-tok"},
            }
        }
        with (
            patch("quarry.client.resolver.read_proxy_config", return_value=login),
            patch.object(ClientConfig, "loopback_token") as token,
        ):
            TargetResolver.resolve()
        token.assert_not_called()
