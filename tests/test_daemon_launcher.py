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

    @pytest.mark.parametrize("bad_key", ["abc def", "a\tb", "sk\nx", "a b c"])
    def test_internal_whitespace_key_refused_at_start(self, bad_key: str) -> None:
        """A key with INTERNAL whitespace makes "Bearer <key>" split into 3+ parts,
        so no client can ever authenticate — fail closed at start, not a silently
        401-forever daemon.  Rejected during construction (the _normalized seam)."""
        with pytest.raises(SystemExit, match="must not contain whitespace"):
            DaemonLauncher(_options(api_key=bad_key))

    def test_leading_trailing_whitespace_key_round_trips(self) -> None:
        """Outer whitespace is stripped (matching the daemon), so a clean key with
        a trailing newline still boots with the stripped value — only INTERNAL
        whitespace is fatal."""
        launcher = DaemonLauncher(_options(api_key="  sk-abc123  "))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].api_key == "sk-abc123"

    def test_whitespace_only_key_still_mints_on_loopback(self) -> None:
        """A whitespace-only key is absent (-> None), NOT internal-whitespace: a
        loopback bind still mints its token rather than being refused."""
        launcher = DaemonLauncher(_options(host="127.0.0.1", api_key="   "))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        config = mock_serve.call_args[0][1]
        assert config.api_key and config.api_key.strip()  # a minted token
        assert len(config.api_key) >= 32

    def test_loopback_name_host_canonicalized_to_literal(self) -> None:
        """`quarryd --host localhost` must bind the literal 127.0.0.1.

        The launcher is the actual bind point, so a DIRECT invocation (not only
        the managed service unit) agrees with the install probe and login target,
        which use 127.0.0.1.  Binding the NAME would land on ::1 on an
        IPv6-preferring host while the client checks 127.0.0.1 (false timeout + 401).
        """
        launcher = DaemonLauncher(_options(host="localhost", api_key=None))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        config = mock_serve.call_args[0][1]
        assert config.host == "127.0.0.1"  # not "localhost"
        # The key gate ran on the canonical host: localhost is loopback, so a
        # loopback token was minted and no operator key was demanded.
        assert config.api_key  # minted, not refused

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
    def test_literal_loopback_host_unchanged(self, host: str) -> None:
        """An explicit loopback literal is left as the operator set it."""
        launcher = DaemonLauncher(_options(host=host, api_key=None))
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].host == host

    def test_non_loopback_host_unchanged(self, tmp_path: Path) -> None:
        """A non-loopback 0.0.0.0 is left unchanged (with a key and TLS)."""
        (tmp_path / "server.crt").write_text("CERT")
        (tmp_path / "server.key").write_text("KEY")
        launcher = DaemonLauncher(
            _options(host="0.0.0.0", api_key="k", tls=True)  # noqa: S104
        )
        with (
            patch("quarry.daemon.launcher.TLS_DIR", tmp_path),
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        assert mock_serve.call_args[0][1].host == "0.0.0.0"  # noqa: S104

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

    def test_non_loopback_without_tls_refuses(self) -> None:
        """A keyed but plaintext network bind ships content in the clear — refuse.

        A key authenticates but does not encrypt; a non-loopback bind must carry
        TLS so remembered notes and raw transcripts never cross the wire in
        cleartext.  The refusal fires even though an operator key is present.
        """
        launcher = DaemonLauncher(
            _options(host="0.0.0.0", api_key="k", tls=False)  # noqa: S104
        )
        with (
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
            pytest.raises(SystemExit, match="without TLS"),
        ):
            launcher.launch()
        mock_serve.assert_not_called()

    def test_non_loopback_with_key_and_tls_serves(self, tmp_path: Path) -> None:
        """A non-loopback bind serves once it has BOTH a key and TLS."""
        (tmp_path / "server.crt").write_text("CERT")
        (tmp_path / "server.key").write_text("KEY")
        launcher = DaemonLauncher(
            _options(host="0.0.0.0", api_key="k", tls=True)  # noqa: S104
        )
        with (
            patch("quarry.daemon.launcher.TLS_DIR", tmp_path),
            patch("quarry.daemon.launcher.DaemonServer.serve") as mock_serve,
            patch("quarry.daemon.launcher.Settings"),
        ):
            launcher.launch()
        config = mock_serve.call_args[0][1]
        assert config.api_key == "k"
        assert config.ssl_certfile == str(tmp_path / "server.crt")

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
