"""Tests for quarry.service — daemon lifecycle management."""

from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.config import DEFAULT_PORT
from quarry.service import (
    _LABEL,
    _get_tls_hostname,
    _launchd_plist_content,
    _quarry_exec_args,
    _systemd_unit_content,
    detect_platform,
    install,
    uninstall,
)

# Patch write_tls_files across all install() calls to avoid writing to
# ~/.punt-labs/quarry/tls/ during tests.
_PATCH_TLS = patch("quarry.service.write_tls_files")
_PATCH_CERT_FP = patch("quarry.service.cert_fingerprint", return_value="")


class TestGetTlsHostname:
    def test_env_var_takes_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUARRY_TLS_HOSTNAME", "my.server.example.com")
        assert _get_tls_hostname() == "my.server.example.com"

    def test_fqdn_used_when_has_dot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QUARRY_TLS_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.getfqdn", lambda: "server.example.com")
        assert _get_tls_hostname() == "server.example.com"

    def test_gethostname_fallback_when_no_dot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QUARRY_TLS_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.getfqdn", lambda: "server")
        monkeypatch.setattr("socket.gethostname", lambda: "server")
        assert _get_tls_hostname() == "server"


class TestDetectPlatform:
    def test_darwin(self) -> None:
        with patch.object(platform, "system", return_value="Darwin"):
            assert detect_platform() == "macos"

    def test_linux(self) -> None:
        with patch.object(platform, "system", return_value="Linux"):
            assert detect_platform() == "linux"

    def test_unsupported(self) -> None:
        with (
            patch.object(platform, "system", return_value="Windows"),
            pytest.raises(SystemExit, match="Unsupported platform"),
        ):
            detect_platform()


class TestQuarryExecArgs:
    def test_prefers_uv_tool_binary(self, tmp_path: Path) -> None:
        fake_bin = tmp_path / "quarry"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        local_bin = tmp_path / ".local" / "bin" / "quarry"
        local_bin.parent.mkdir(parents=True, exist_ok=True)
        local_bin.symlink_to(fake_bin)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        # No TLS certs present → no --tls flag
        assert args[-3:] == ["serve", "--port", str(DEFAULT_PORT)]
        assert str(fake_bin) == args[0]

    def test_falls_back_to_sys_executable(self, tmp_path: Path) -> None:
        import sys

        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            # No ~/.local/bin/quarry exists in tmp_path
            args = _quarry_exec_args()
        assert args[0] == sys.executable
        assert args[1:] == ["-m", "quarry", "serve", "--port", str(DEFAULT_PORT)]

    def test_appends_tls_flag_when_cert_and_key_exist(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "server.crt").write_text("CERT")
        (tls_dir / "server.key").write_text("KEY")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tls_dir),
        ):
            args = _quarry_exec_args()
        assert "--tls" in args

    def test_no_tls_flag_on_partial_state(self, tmp_path: Path) -> None:
        """Only server.crt present (no key) → no --tls flag, warning logged."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "server.crt").write_text("CERT")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tls_dir),
        ):
            args = _quarry_exec_args()
        assert "--tls" not in args

    def test_quarry_serve_host_env_adds_host_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_SERVE_HOST set must produce --host <value> in args."""
        bind_addr = "0.0.0.0"  # noqa: S104
        monkeypatch.setenv("QUARRY_SERVE_HOST", bind_addr)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--host" in args
        host_idx = args.index("--host")
        assert args[host_idx + 1] == bind_addr

    def test_quarry_serve_host_unset_no_host_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When QUARRY_SERVE_HOST is unset, --host must not appear in args."""
        monkeypatch.delenv("QUARRY_SERVE_HOST", raising=False)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--host" not in args

    def test_quarry_serve_host_empty_no_host_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty QUARRY_SERVE_HOST must not produce --host in args."""
        monkeypatch.setenv("QUARRY_SERVE_HOST", "")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--host" not in args

    def test_quarry_exec_args_includes_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_API_KEY set must produce --api-key <value> as adjacent elements."""
        monkeypatch.setenv("QUARRY_API_KEY", "testkey")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--api-key" in args
        key_idx = args.index("--api-key")
        assert args[key_idx + 1] == "testkey"

    def test_quarry_exec_args_no_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When QUARRY_API_KEY is absent, --api-key must not appear in args."""
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--api-key" not in args


class TestServiceFileApiKey:
    def test_launchd_plist_includes_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_launchd_plist_content() must embed --api-key and the key value."""
        monkeypatch.setenv("QUARRY_API_KEY", "k1")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            content = _launchd_plist_content()
        assert "--api-key" in content
        assert "k1" in content

    def test_systemd_unit_includes_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_systemd_unit_content() must embed --api-key and the key value."""
        monkeypatch.setenv("QUARRY_API_KEY", "k1")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            content = _systemd_unit_content()
        assert "--api-key" in content
        assert "k1" in content


class TestInstallMacOS:
    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    def test_fresh_install_writes_plist_and_loads(
        self, _sys: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        with (
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            # First call: launchctl list → not found (fresh install)
            # Second call: launchctl load → success
            # Third call: launchctl list → running
            mock_run.side_effect = [
                MagicMock(returncode=113),  # list: not found
                MagicMock(returncode=0),  # load: success
                MagicMock(returncode=0),  # list: running
            ]

            msg = install()

            assert plist_path.exists()
            content = plist_path.read_text()
            assert _LABEL in content
            assert "KeepAlive" in content
            assert "RunAtLoad" in content
            assert "<string>serve</string>" in content
            assert "running" in msg
            assert str(DEFAULT_PORT) in msg

            # Fresh install: no unload, just load
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert not any("unload" in c for c in calls), (
                "fresh install must not unload"
            )
            assert any("load" in c for c in calls)

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    def test_upgrade_unloads_before_loading(
        self, _sys: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        plist_path.write_text("<plist>old binary</plist>")
        with (
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            # First call: launchctl list → found (existing service)
            # Second call: launchctl unload → success
            # Third call: launchctl load → success
            # Fourth call: launchctl list → running
            mock_run.side_effect = [
                MagicMock(returncode=0),  # list: found (upgrade)
                MagicMock(returncode=0),  # unload: success
                MagicMock(returncode=0),  # load: success
                MagicMock(returncode=0),  # list: running
            ]

            msg = install()

            assert plist_path.exists()
            assert "running" in msg

            # Upgrade: unload before load
            calls = [c.args[0] for c in mock_run.call_args_list]
            unload_idx = next(i for i, c in enumerate(calls) if "unload" in c)
            load_idx = next(i for i, c in enumerate(calls) if "load" in c)
            assert unload_idx < load_idx

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    def test_uninstall_removes_plist(
        self, _sys: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        plist_path.write_text("<plist>test</plist>")
        with (
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
        ):
            msg = uninstall()

            assert not plist_path.exists()
            assert "uninstalled" in msg

            # Verify launchctl unload was called
            unload_call = mock_run.call_args_list[0]
            assert "launchctl" in unload_call.args[0][0]
            assert "unload" in unload_call.args[0]

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    def test_uninstall_noop_when_missing(
        self, _sys: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        with (
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
        ):
            msg = uninstall()

            assert "uninstalled" in msg
            mock_run.assert_not_called()


class TestInstallLinux:
    @patch("quarry.service._has_linger", return_value=True)
    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Linux")
    def test_writes_unit_and_enables(
        self, _sys: MagicMock, mock_run: MagicMock, _linger: MagicMock, tmp_path: Path
    ) -> None:
        unit_path = tmp_path / "quarry.service"
        with (
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n")

            msg = install()

            assert unit_path.exists()
            content = unit_path.read_text()
            assert "Restart=on-failure" in content
            assert "RestartSec=5" in content
            assert "ExecStart=" in content
            assert "running" in msg

            # Verify daemon-reload and enable calls
            assert mock_run.call_count >= 3  # daemon-reload, enable, is-active
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert any("daemon-reload" in c for c in calls)
            assert any("enable" in c for c in calls)

    @patch("quarry.service._has_linger", return_value=False)
    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Linux")
    def test_warns_without_linger(
        self, _sys: MagicMock, mock_run: MagicMock, _linger: MagicMock, tmp_path: Path
    ) -> None:
        unit_path = tmp_path / "quarry.service"
        with (
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n")

            msg = install()

            assert "linger" in msg.lower()
            assert "loginctl enable-linger" in msg

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Linux")
    def test_uninstall_removes_unit(
        self, _sys: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        unit_path = tmp_path / "quarry.service"
        unit_path.write_text("[Unit]\ntest\n")
        with (
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
        ):
            msg = uninstall()

            assert not unit_path.exists()
            assert "uninstalled" in msg

            calls = [c.args[0] for c in mock_run.call_args_list]
            assert any("disable" in c for c in calls)
            assert any("daemon-reload" in c for c in calls)
