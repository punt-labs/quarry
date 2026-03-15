"""Tests for quarry.service — daemon lifecycle management."""

from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.config import DEFAULT_PORT
from quarry.service import (
    _LABEL,
    _quarry_exec_args,
    detect_platform,
    install,
    uninstall,
)


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
    def test_uses_current_python(self) -> None:
        import sys

        args = _quarry_exec_args()
        assert args[0] == sys.executable
        assert args[1:] == ["-m", "quarry", "serve", "--port", str(DEFAULT_PORT)]


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
            assert "<string>-m</string>" in content
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
