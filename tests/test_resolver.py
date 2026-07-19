"""Tier-selection and trust tests for :class:`TargetResolver`.

The trust assertions here are the ones djb hard-gates: the live ``serve.token``
is read ONLY for tier 3 and presented ONLY to the literal loopback IP (never a
name, never a non-loopback target), and the loopback scheme is ``wss`` + pinned
CA when the daemon CA exists, ``ws`` only for a bare plaintext daemon.
"""

from __future__ import annotations

import logging
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
        # Loopback host: plaintext is same-machine and allowed, so this exercises
        # the whitespace-token -> None mapping without tripping the cleartext guard.
        monkeypatch.setenv("QUARRY_URL", "ws://127.0.0.1:9000")
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
        monkeypatch.setenv("QUARRY_URL", "wss://env.example:9000")
        login = {"quarry": {"url": "wss://stored.example:8420"}}
        with patch("quarry.client.resolver.read_proxy_config", return_value=login):
            assert TargetResolver.resolve().url == "wss://env.example:9000"


class TestTier1EnvSecurity:
    """Never transmit a bearer in cleartext to a remote, or to a loopback NAME."""

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

    def test_cleartext_no_token_to_remote_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plaintext remote target is refused even with NO token: it is the
        request CONTENT (remembered notes, transcripts) that must not cross the
        wire in cleartext, not just the bearer."""
        monkeypatch.delenv("QUARRY_TOKEN", raising=False)
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        monkeypatch.setenv("QUARRY_URL", "ws://remote.example:9000")
        with pytest.raises(ClientConfigError, match="cleartext"):
            TargetResolver.resolve()

    def test_loopback_name_is_canonicalized_to_literal_never_the_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ws://localhost + token: the NAME is migrated to 127.0.0.1 BEFORE the
        # bearer is presented, so a dual-stack resolver can never redirect the
        # token to a co-tenant's ::1.  The token never goes to "localhost".
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        monkeypatch.setenv("QUARRY_URL", "ws://localhost:9000")
        monkeypatch.setenv("QUARRY_TOKEN", "tok")
        cfg = TargetResolver.resolve()
        assert cfg.url == "ws://127.0.0.1:9000"
        assert "localhost" not in cfg.url
        bearer = cfg.token
        assert bearer == "tok"

    def test_url_with_no_host_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A QUARRY_URL with no host (ws://:9000) must error, not silently fall
        # back to a localhost/empty target — validate at the boundary.
        monkeypatch.delenv("QUARRY_TOKEN", raising=False)
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        monkeypatch.setenv("QUARRY_URL", "ws://:9000")
        with pytest.raises(ClientConfigError, match="no host"):
            TargetResolver.resolve()

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

    def test_malformed_toml_warns_and_falls_through_to_loopback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Class 2 + no-split-horizon: a malformed remote config must not crash —
        # it falls through to the loopback default — but the operator's ignored
        # config is surfaced with a warning, not silently.
        _no_env(monkeypatch)
        ca = tmp_path / "ca.crt"
        ca.write_text("x")
        with (
            caplog.at_level(logging.WARNING, logger="quarry.client.resolver"),
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
        assert "malformed quarry.toml" in caplog.text

    def test_unreadable_toml_warns_and_falls_through_to_loopback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Class 2: an EXISTING but unreadable quarry.toml (permissions / IO) raises
        # OSError from read_proxy_config — it must warn and fall through to the
        # loopback default, never crash the CLI.
        _no_env(monkeypatch)
        ca = tmp_path / "ca.crt"
        ca.write_text("x")
        with (
            caplog.at_level(logging.WARNING, logger="quarry.client.resolver"),
            patch(
                "quarry.client.resolver.read_proxy_config",
                side_effect=PermissionError("quarry.toml not readable"),
            ),
            patch("quarry.client.resolver._DAEMON_CA_PATH", ca),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value="live"),
        ):
            cfg = TargetResolver.resolve()
        assert cfg.url == "wss://127.0.0.1:8420"
        assert "unreadable/malformed quarry.toml" in caplog.text

    def test_loopback_login_daemon_down_raises_connection_error_with_nudge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stored loopback login whose quarryd is DOWN (no serve.port) surfaces
        # the same typed QuarryConnectionError + autostart nudge as tier 3, not a
        # raw ClientConfigError with no guidance.
        _no_env(monkeypatch)
        login = {"quarry": {"url": "wss://127.0.0.1:8420"}}
        down = _run_dir(0)
        down.port_file.read.side_effect = FileNotFoundError("no serve.port")
        with (
            patch("quarry.client.resolver.read_proxy_config", return_value=login),
            patch.object(
                ClientConfig,
                "_serve_token",
                side_effect=ClientConfigError("serve.token missing"),
            ),
            patch.object(ClientConfig, "active_run_dir", return_value=down),
            pytest.raises(QuarryConnectionError) as info,
        ):
            TargetResolver.resolve()
        assert "quarryd is not running" in info.value.message
        assert info.value.target == "127.0.0.1"

    def test_loopback_login_token_unreadable_gives_token_specific_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # serve.port present (daemon UP) but the stored-login serve.token read
        # fails: tier 2 surfaces the SAME token-specific guidance as tier 3, not
        # the daemon-down autostart message.
        _no_env(monkeypatch)
        login = {"quarry": {"url": "wss://127.0.0.1:8420"}}
        with (
            patch("quarry.client.resolver.read_proxy_config", return_value=login),
            patch.object(
                ClientConfig,
                "_serve_token",
                side_effect=ClientConfigError("serve.token unreadable"),
            ),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            pytest.raises(QuarryConnectionError) as info,
        ):
            TargetResolver.resolve()
        assert "serve.token is unreadable or stale" in info.value.message
        assert "not running" not in info.value.message

    def test_stored_url_with_no_host_warns_and_falls_through_to_loopback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A stored url with no host would default to localhost in httpx, silently
        # redirecting the STORED BEARER there. It must warn and fall back to the
        # loopback default (live token), never present the stored bearer to it.
        _no_env(monkeypatch)
        login = {
            "quarry": {
                "url": "ws://:9000",
                "headers": {"Authorization": "Bearer stored-tok"},
            }
        }
        with (
            caplog.at_level(logging.WARNING, logger="quarry.client.resolver"),
            patch("quarry.client.resolver.read_proxy_config", return_value=login),
            patch("quarry.client.resolver._DAEMON_CA_PATH", tmp_path / "absent.crt"),
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value="live"),
        ):
            cfg = TargetResolver.resolve()
        assert cfg.url == "ws://127.0.0.1:8420"
        assert "no host in url" in caplog.text
        # The loopback live token, NOT the stored bearer sent to a localhost.
        bearer = cfg.token
        assert bearer == "live"


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

    def test_unreadable_token_gives_token_specific_message_not_daemon_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # serve.port present (daemon UP) but token unreadable/empty -> fail closed
        # with the TOKEN-specific cause + 'quarry doctor', NOT the daemon-down
        # autostart message (the multi-user case that matters).
        _no_env(monkeypatch)
        with (
            patch.object(ClientConfig, "active_run_dir", return_value=_run_dir(8420)),
            patch.object(ClientConfig, "loopback_token", return_value=None),
            pytest.raises(QuarryConnectionError) as info,
        ):
            TargetResolver.resolve()
        assert "serve.token is unreadable or stale" in info.value.message
        assert "quarry doctor" in info.value.message
        assert "not running" not in info.value.message


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
