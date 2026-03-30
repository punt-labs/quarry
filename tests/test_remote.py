"""Unit tests for quarry.remote."""

from __future__ import annotations

import stat
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.remote import (
    delete_proxy_config,
    mask_token,
    read_proxy_config,
    validate_connection,
    validate_connection_from_ws_url,
    write_proxy_config,
)


@pytest.fixture
def proxy_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".punt-labs" / "mcp-proxy" / "quarry.toml"
    monkeypatch.setattr("quarry.remote.MCP_PROXY_CONFIG_PATH", path)
    return path


class TestWriteProxyConfig:
    def test_creates_file(self, proxy_config_path: Path) -> None:
        write_proxy_config("ws://host:8420/mcp", "sk-test")
        assert proxy_config_path.exists()
        content = proxy_config_path.read_text()
        assert "[quarry]" in content
        assert 'url = "ws://host:8420/mcp"' in content
        assert 'Authorization = "Bearer sk-test"' in content

    def test_chmod_0600(self, proxy_config_path: Path) -> None:
        write_proxy_config("ws://host:8420/mcp", "sk-test")
        mode = stat.S_IMODE(proxy_config_path.stat().st_mode)
        assert mode == 0o600

    def test_creates_parent_dir(self, proxy_config_path: Path) -> None:
        assert not proxy_config_path.parent.exists()
        write_proxy_config("ws://host:8420/mcp", "sk-test")
        assert proxy_config_path.parent.exists()

    def test_overwrites_existing(self, proxy_config_path: Path) -> None:
        write_proxy_config("ws://host:8420/mcp", "sk-first")
        write_proxy_config("ws://host:8420/mcp", "sk-second")
        content = proxy_config_path.read_text()
        assert "sk-second" in content
        assert "sk-first" not in content

    def test_escapes_special_chars_in_token(self, proxy_config_path: Path) -> None:
        token = 'tok"en\\val'  # noqa: S105
        write_proxy_config("ws://host:8420/mcp", token)
        data = read_proxy_config()
        assert data["quarry"]["headers"]["Authorization"] == f"Bearer {token}"


class TestReadProxyConfig:
    def test_returns_empty_when_missing(self, proxy_config_path: Path) -> None:
        assert not proxy_config_path.exists()
        result = read_proxy_config()
        assert result == {}

    def test_reads_url_and_header(self, proxy_config_path: Path) -> None:
        write_proxy_config("ws://host:8420/mcp", "sk-test")
        data = read_proxy_config()
        assert data["quarry"]["url"] == "ws://host:8420/mcp"
        assert data["quarry"]["headers"]["Authorization"] == "Bearer sk-test"

    def test_malformed_toml_raises(self, proxy_config_path: Path) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_config_path.write_text("not valid toml ][[\n")
        with pytest.raises(ValueError, match=str(proxy_config_path)):
            read_proxy_config()


class TestDeleteProxyConfig:
    def test_returns_false_when_missing(self, proxy_config_path: Path) -> None:
        assert not proxy_config_path.exists()
        result = delete_proxy_config()
        assert result is False

    def test_deletes_file_when_only_quarry_section(
        self, proxy_config_path: Path
    ) -> None:
        write_proxy_config("ws://host:8420/mcp", "sk-test")
        assert proxy_config_path.exists()
        result = delete_proxy_config()
        assert result is True
        assert not proxy_config_path.exists()

    def test_removes_quarry_section_when_others_present(
        self, proxy_config_path: Path
    ) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[quarry]\n"
            'url = "ws://host:8420/mcp"\n'
            "\n"
            "[quarry.headers]\n"
            'Authorization = "Bearer sk-test"\n'
            "\n"
            "[other]\n"
            'foo = "bar"\n'
        )
        proxy_config_path.write_text(content)
        result = delete_proxy_config()
        assert result is True
        assert proxy_config_path.exists()
        remaining = proxy_config_path.read_text()
        assert "[quarry]" not in remaining
        assert "[other]" in remaining
        assert 'foo = "bar"' in remaining
        assert tomllib.loads(remaining)  # remaining TOML parses without error

    def test_returns_false_when_no_quarry_section(
        self, proxy_config_path: Path
    ) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        original = '[other]\nfoo = "bar"\n'
        proxy_config_path.write_text(original)
        result = delete_proxy_config()
        assert result is False
        assert proxy_config_path.read_text() == original

    def test_does_not_eat_dotted_sibling_sections(
        self, proxy_config_path: Path
    ) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[quarry]\n"
            'url = "ws://host:8420/mcp"\n'
            "\n"
            "[quarry.headers]\n"
            'Authorization = "Bearer sk-test"\n'
            "\n"
            "[proxy.settings]\n"
            'foo = "bar"\n'
        )
        proxy_config_path.write_text(content)
        result = delete_proxy_config()
        assert result is True
        assert proxy_config_path.exists()
        remaining = proxy_config_path.read_text()
        assert "[quarry]" not in remaining
        assert "[proxy.settings]" in remaining
        assert tomllib.loads(remaining)["proxy"]["settings"]["foo"] == "bar"


class TestValidateConnection:
    def test_success_returns_true(self) -> None:
        mock_response = MagicMock()
        with patch("urllib.request.urlopen", return_value=mock_response):
            ok, reason = validate_connection("localhost", 8420, "sk-test")
        assert ok is True
        assert reason == ""

    def test_401_returns_false(self) -> None:
        exc = urllib.error.HTTPError(
            url="http://localhost:8420/status",
            code=401,
            msg="Unauthorized",
            hdrs=MagicMock(),
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, reason = validate_connection("localhost", 8420, "bad-key")
        assert ok is False
        assert "Authentication failed" in reason

    def test_500_returns_false(self) -> None:
        exc = urllib.error.HTTPError(
            url="http://localhost:8420/status",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, reason = validate_connection("localhost", 8420, "sk-test")
        assert ok is False
        assert "Server returned 500" in reason

    def test_url_error_returns_false(self) -> None:
        exc = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, reason = validate_connection("localhost", 8420, "sk-test")
        assert ok is False
        assert "Could not connect to localhost:8420" in reason

    def test_timeout_returns_false(self) -> None:
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            ok, reason = validate_connection("localhost", 8420, "sk-test")
        assert ok is False
        assert "Could not connect to localhost:8420" in reason


class TestValidateConnectionFromWsUrl:
    def test_wss_url_uses_https_scheme(self) -> None:
        captured: list[str] = []

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = lambda s: MagicMock()
        mock_ctx.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, timeout: int) -> MagicMock:
            captured.append(req.full_url)
            return mock_ctx

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            validate_connection_from_ws_url("wss://host:8420/mcp", "token")

        assert len(captured) == 1
        assert captured[0].startswith("https://"), (
            f"Expected https:// URL but got: {captured[0]}"
        )


class TestMaskToken:
    def test_short_token(self) -> None:
        assert mask_token("abc") == "****"

    def test_four_chars(self) -> None:
        assert mask_token("abcd") == "abcd****"

    def test_normal_token(self) -> None:
        assert mask_token("sk-abcdef") == "sk-a****"
