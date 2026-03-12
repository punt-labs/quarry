"""Tests for quarry.proxy — mcp-proxy binary download and install."""

from __future__ import annotations

import hashlib
import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.proxy import _BINARY_NAME, _asset_name, install, installed_path


class TestAssetName:
    def test_darwin_arm64(self) -> None:
        with (
            patch.object(platform, "system", return_value="Darwin"),
            patch.object(platform, "machine", return_value="arm64"),
        ):
            assert _asset_name() == "mcp-proxy-darwin-arm64"

    def test_darwin_x86(self) -> None:
        with (
            patch.object(platform, "system", return_value="Darwin"),
            patch.object(platform, "machine", return_value="x86_64"),
        ):
            assert _asset_name() == "mcp-proxy-darwin-amd64"

    def test_linux_aarch64(self) -> None:
        with (
            patch.object(platform, "system", return_value="Linux"),
            patch.object(platform, "machine", return_value="aarch64"),
        ):
            assert _asset_name() == "mcp-proxy-linux-arm64"

    def test_linux_amd64(self) -> None:
        with (
            patch.object(platform, "system", return_value="Linux"),
            patch.object(platform, "machine", return_value="x86_64"),
        ):
            assert _asset_name() == "mcp-proxy-linux-amd64"

    def test_unsupported_platform(self) -> None:
        with (
            patch.object(platform, "system", return_value="Windows"),
            pytest.raises(SystemExit, match="Unsupported platform"),
        ):
            _asset_name()

    def test_unsupported_arch(self) -> None:
        with (
            patch.object(platform, "system", return_value="Darwin"),
            patch.object(platform, "machine", return_value="ppc64"),
            pytest.raises(SystemExit, match="Unsupported architecture"),
        ):
            _asset_name()


class TestInstalledPath:
    def test_found(self) -> None:
        path = "/usr/local/bin/mcp-proxy"
        with patch("quarry.proxy.shutil.which", return_value=path):
            assert installed_path() == path

    def test_not_found(self) -> None:
        with patch("quarry.proxy.shutil.which", return_value=None):
            assert installed_path() is None


class TestInstall:
    @staticmethod
    def _make_mock_resp(data: bytes) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = data
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_downloads_and_installs(self, tmp_path: Path) -> None:
        binary_content = b"\x00" * 100
        real_checksum = hashlib.sha256(binary_content).hexdigest()
        checksum_text = f"{real_checksum}  mcp-proxy-darwin-arm64\n"

        binary_resp = self._make_mock_resp(binary_content)
        checksum_resp = self._make_mock_resp(checksum_text.encode())

        with (
            patch("quarry.proxy._INSTALL_DIR", tmp_path),
            patch(
                "quarry.proxy._asset_name",
                return_value="mcp-proxy-darwin-arm64",
            ),
            patch(
                "quarry.proxy.urllib.request.urlopen",
                side_effect=[binary_resp, checksum_resp],
            ),
        ):
            result = install(version="v0.1.0")

        assert "v0.1.0" in result
        dest = tmp_path / _BINARY_NAME
        assert dest.exists()
        assert dest.stat().st_mode & 0o111  # executable

    def test_checksum_mismatch_deletes_binary(self, tmp_path: Path) -> None:
        binary_content = b"\x00" * 100
        bad_checksum = "0" * 64
        checksum_text = f"{bad_checksum}  mcp-proxy-darwin-arm64\n"

        binary_resp = self._make_mock_resp(binary_content)
        checksum_resp = self._make_mock_resp(checksum_text.encode())

        with (
            patch("quarry.proxy._INSTALL_DIR", tmp_path),
            patch(
                "quarry.proxy._asset_name",
                return_value="mcp-proxy-darwin-arm64",
            ),
            patch(
                "quarry.proxy.urllib.request.urlopen",
                side_effect=[binary_resp, checksum_resp],
            ),
            pytest.raises(ValueError, match="Checksum mismatch"),
        ):
            install(version="v0.1.0")

        # Neither final binary nor tempfile should remain
        assert not (tmp_path / _BINARY_NAME).exists()
        assert not list(tmp_path.glob("*.tmp"))
