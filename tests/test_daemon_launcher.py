"""Tests for the quarryd launcher: bind-key policy, token minting, TLS paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.daemon.launcher import BindOptions, DaemonLauncher


def _options(**overrides: object) -> BindOptions:
    base: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8420,
        "db": "",
        "api_key": None,
        "cors_origins": (),
        "tls": False,
    }
    base.update(overrides)
    return BindOptions(**base)  # type: ignore[arg-type]


class TestLaunch:
    def test_loopback_mints_token_when_no_key(self) -> None:
        """A loopback bind with no operator key still serves — with a minted key."""
        launcher = DaemonLauncher(_options(api_key=None))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        config = mock_serve.call_args[0][1]
        assert config.api_key  # a non-empty minted token
        assert len(config.api_key) >= 32

    def test_operator_key_is_used_verbatim(self) -> None:
        launcher = DaemonLauncher(_options(api_key="operator-set-key"))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].api_key == "operator-set-key"

    def test_non_loopback_without_key_refuses(self) -> None:
        """A network bind on an auto-token is false security — refuse to bind."""
        launcher = DaemonLauncher(_options(host="0.0.0.0", api_key=None))  # noqa: S104
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
            pytest.raises(SystemExit, match="Refusing to bind"),
        ):
            launcher.launch()
        mock_serve.assert_not_called()

    def test_non_loopback_with_key_serves(self) -> None:
        launcher = DaemonLauncher(_options(host="0.0.0.0", api_key="k"))  # noqa: S104
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].api_key == "k"

    def test_whitespace_key_loopback_mints(self) -> None:
        """A whitespace-only key is absent: a loopback bind MINTS and starts
        (does not exit at the daemon boundary)."""
        launcher = DaemonLauncher(_options(host="127.0.0.1", api_key="   "))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        config = mock_serve.call_args[0][1]
        assert config.api_key and config.api_key.strip()  # a real minted token
        assert len(config.api_key) >= 32

    def test_whitespace_key_non_loopback_refuses_at_gate(self) -> None:
        """A whitespace-only key is absent: a network bind is refused AT the bind
        gate (enforce_bind_key), not later at the daemon boundary."""
        launcher = DaemonLauncher(_options(host="0.0.0.0", api_key="   "))  # noqa: S104
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
            pytest.raises(SystemExit, match="Refusing to bind"),
        ):
            launcher.launch()
        mock_serve.assert_not_called()

    def test_minted_tokens_differ_between_launches(self) -> None:
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            DaemonLauncher(_options()).launch()
            DaemonLauncher(_options()).launch()
        first = mock_serve.call_args_list[0][0][1].api_key
        second = mock_serve.call_args_list[1][0][1].api_key
        assert first != second


class TestTlsPaths:
    def test_tls_missing_certs_exits(self, tmp_path: Path) -> None:
        launcher = DaemonLauncher(_options(tls=True))
        with (
            patch("quarry.daemon.launcher.TLS_DIR", tmp_path),
            pytest.raises(SystemExit, match="TLS certificate files not found"),
        ):
            launcher.launch()

    def test_tls_present_passes_paths(self, tmp_path: Path) -> None:
        (tmp_path / "server.crt").write_text("CERT")
        (tmp_path / "server.key").write_text("KEY")
        launcher = DaemonLauncher(_options(tls=True))
        with (
            patch("quarry.daemon.launcher.TLS_DIR", tmp_path),
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        config = mock_serve.call_args[0][1]
        assert config.ssl_certfile == str(tmp_path / "server.crt")
        assert config.ssl_keyfile == str(tmp_path / "server.key")


class TestCorsOrigins:
    def test_empty_cors_is_none(self) -> None:
        launcher = DaemonLauncher(_options(cors_origins=()))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].cors_origins is None

    def test_cors_origins_forwarded(self) -> None:
        launcher = DaemonLauncher(_options(cors_origins=("http://a", "http://b")))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].cors_origins == frozenset(
            {"http://a", "http://b"}
        )
