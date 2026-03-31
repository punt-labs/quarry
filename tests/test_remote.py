"""Unit tests for quarry.remote."""

from __future__ import annotations

import http.client
import os
import ssl
import stat
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.remote import (
    CA_CERT_PATH,
    delete_proxy_config,
    fetch_ca_cert,
    mask_token,
    read_proxy_config,
    store_ca_cert,
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

    def test_token_none_omits_auth_header(self, proxy_config_path: Path) -> None:
        """When token=None the [quarry.headers] section must not appear."""
        write_proxy_config("wss://host:8420/mcp", None)
        content = proxy_config_path.read_text()
        assert "[quarry]" in content
        assert "Authorization" not in content
        assert "[quarry.headers]" not in content

    def test_token_none_parses_as_valid_toml(self, proxy_config_path: Path) -> None:
        """Config written without a token must parse cleanly."""
        write_proxy_config("wss://host:8420/mcp", None)
        data = read_proxy_config()
        assert data["quarry"]["url"] == "wss://host:8420/mcp"
        assert "headers" not in data["quarry"]


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

    def test_token_none_sends_no_auth_header(self) -> None:
        """When token=None no Authorization header is sent."""
        captured_requests: list[urllib.request.Request] = []
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = lambda s: MagicMock()
        mock_ctx.__exit__ = MagicMock(return_value=False)

        def capture_urlopen(
            req: urllib.request.Request,
            timeout: int,
            context: object = None,
        ) -> MagicMock:
            captured_requests.append(req)
            return mock_ctx

        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            ok, _ = validate_connection("localhost", 8420, None)

        assert ok is True
        assert len(captured_requests) == 1
        assert "Authorization" not in captured_requests[0].headers


class TestValidateConnectionFromWsUrl:
    def test_wss_url_uses_https_scheme(self) -> None:
        captured: list[str] = []

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = lambda s: MagicMock()
        mock_ctx.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(
            req: urllib.request.Request,
            timeout: int,
            context: object = None,
        ) -> MagicMock:
            captured.append(req.full_url)
            return mock_ctx

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            validate_connection_from_ws_url("wss://host:8420/mcp", "token")

        assert len(captured) == 1
        assert captured[0].startswith("https://"), (
            f"Expected https:// URL but got: {captured[0]}"
        )

    def test_ca_cert_path_forwarded(self, tmp_path: Path) -> None:
        """ca_cert_path must reach urlopen as a non-None SSL context."""
        from quarry.tls import generate_ca

        ca_pem, _ = generate_ca("host.example.com")
        ca_file = tmp_path / "ca.crt"
        ca_file.write_bytes(ca_pem)

        captured_contexts: list[object] = []

        def fake_urlopen(
            req: object, *, timeout: int, context: object = None
        ) -> object:
            captured_contexts.append(context)
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            validate_connection_from_ws_url(
                "wss://host.example.com:8420/mcp", "tok", str(ca_file)
            )

        assert len(captured_contexts) == 1
        assert captured_contexts[0] is not None, (
            "ca_cert_path must produce a non-None SSL context"
        )


class TestMaskToken:
    def test_short_token(self) -> None:
        assert mask_token("abc") == "****"

    def test_four_chars(self) -> None:
        assert mask_token("abcd") == "abcd****"

    def test_normal_token(self) -> None:
        assert mask_token("sk-abcdef") == "sk-a****"


class TestWriteProxyConfigWithCaCert:
    def test_ca_cert_included_when_provided(self, proxy_config_path: Path) -> None:
        write_proxy_config("wss://host:8420/mcp", "sk-test", "/path/to/ca.crt")
        content = proxy_config_path.read_text()
        assert 'ca_cert = "/path/to/ca.crt"' in content

    def test_ca_cert_omitted_when_none(self, proxy_config_path: Path) -> None:
        write_proxy_config("wss://host:8420/mcp", "sk-test")
        content = proxy_config_path.read_text()
        assert "ca_cert" not in content

    def test_ca_cert_parses_as_toml(self, proxy_config_path: Path) -> None:
        write_proxy_config("wss://host:8420/mcp", "sk-test", "/path/to/ca.crt")
        data = read_proxy_config()
        assert data["quarry"]["ca_cert"] == "/path/to/ca.crt"
        assert data["quarry"]["url"] == "wss://host:8420/mcp"

    def test_ca_cert_path_with_special_chars(self, proxy_config_path: Path) -> None:
        path_with_quotes = '/path/to/"tricky"/ca.crt'
        write_proxy_config("wss://host:8420/mcp", "sk-test", path_with_quotes)
        data = read_proxy_config()
        assert data["quarry"]["ca_cert"] == path_with_quotes


class TestFetchCaCert:
    def _make_conn(self, status: int, body: bytes) -> MagicMock:
        """Return a mock HTTPSConnection whose getresponse() yields status and body."""
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = body
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.getresponse.return_value = mock_resp
        return mock_conn

    def test_success(self) -> None:
        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        mock_conn = self._make_conn(200, pem)
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            result = fetch_ca_cert("host.example.com", 8420)
        assert result == pem

    def test_404_raises_value_error(self) -> None:
        mock_conn = self._make_conn(404, b"Not Found")
        with (
            patch("http.client.HTTPSConnection", return_value=mock_conn),
            pytest.raises(ValueError, match="no CA certificate"),
        ):
            fetch_ca_cert("host.example.com", 8420)

    def test_other_http_error_raises_value_error(self) -> None:
        mock_conn = self._make_conn(500, b"Internal Server Error")
        with (
            patch("http.client.HTTPSConnection", return_value=mock_conn),
            pytest.raises(ValueError, match="HTTP 500"),
        ):
            fetch_ca_cert("host.example.com", 8420)

    def test_connection_error_raises_value_error(self) -> None:
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.request.side_effect = OSError("connection refused")
        with (
            patch("http.client.HTTPSConnection", return_value=mock_conn),
            pytest.raises(ValueError, match="Could not reach"),
        ):
            fetch_ca_cert("host.example.com", 8420)

    def test_non_pem_response_raises_value_error(self) -> None:
        mock_conn = self._make_conn(200, b"not a certificate")
        with (
            patch("http.client.HTTPSConnection", return_value=mock_conn),
            pytest.raises(ValueError, match="unexpected data"),
        ):
            fetch_ca_cert("host.example.com", 8420)

    def test_uses_https_not_http(self) -> None:
        """Fetch must use HTTPSConnection (TOFU bootstrap, HTTPS with verify=False)."""
        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        mock_conn = self._make_conn(200, pem)

        with patch("http.client.HTTPSConnection", return_value=mock_conn) as mock_cls:
            fetch_ca_cert("host.example.com", 8420)

        # HTTPSConnection must be constructed — plain HTTPConnection must not be used.
        mock_cls.assert_called_once()
        call_args = mock_cls.call_args
        assert call_args.args[0] == "host.example.com"
        assert call_args.args[1] == 8420

    def test_ssl_context_disables_verification(self) -> None:
        """TOFU bootstrap must connect with check_hostname=False and CERT_NONE."""
        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        mock_conn = self._make_conn(200, pem)
        captured_contexts: list[ssl.SSLContext] = []

        def capture_cls(
            host: str, port: int, context: ssl.SSLContext, timeout: int
        ) -> MagicMock:
            captured_contexts.append(context)
            return mock_conn

        with patch("http.client.HTTPSConnection", side_effect=capture_cls):
            fetch_ca_cert("host.example.com", 8420)

        assert len(captured_contexts) == 1
        ctx = captured_contexts[0]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_conn_closed_on_success(self) -> None:
        """Connection must be closed after a successful fetch."""
        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        mock_conn = self._make_conn(200, pem)
        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            fetch_ca_cert("host.example.com", 8420)
        mock_conn.close.assert_called_once()

    def test_conn_closed_on_error(self) -> None:
        """Connection must be closed even when the request raises OSError."""
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.request.side_effect = OSError("timeout")
        with (
            patch("http.client.HTTPSConnection", return_value=mock_conn),
            pytest.raises(ValueError, match="Could not reach"),
        ):
            fetch_ca_cert("host.example.com", 8420)
        mock_conn.close.assert_called_once()


class TestStoreCaCert:
    def test_writes_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ca_path = tmp_path / "quarry-ca.crt"
        monkeypatch.setattr("quarry.remote.CA_CERT_PATH", ca_path)
        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        store_ca_cert(pem)
        assert ca_path.read_bytes() == pem

    def test_chmod_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ca_path = tmp_path / "quarry-ca.crt"
        monkeypatch.setattr("quarry.remote.CA_CERT_PATH", ca_path)
        store_ca_cert(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        mode = stat.S_IMODE(ca_path.stat().st_mode)
        assert mode == 0o600

    def test_creates_parent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca_path = tmp_path / "subdir" / "quarry-ca.crt"
        monkeypatch.setattr("quarry.remote.CA_CERT_PATH", ca_path)
        store_ca_cert(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        assert ca_path.exists()

    def test_default_ca_cert_path(self) -> None:
        expected = Path.home() / ".punt-labs" / "mcp-proxy" / "quarry-ca.crt"
        assert expected == CA_CERT_PATH


class TestWriteProxyConfigFdLeak:
    """Verify write_proxy_config() closes fd and removes .tmp on all failure paths."""

    def test_fd_closed_and_tmp_removed_when_fdopen_raises(
        self, proxy_config_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = proxy_config_path.with_suffix(".tmp")
        captured_fds: list[int] = []
        closed_fds: list[int] = []

        real_open = os.open

        def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
            fd = real_open(path, flags, mode)
            captured_fds.append(fd)
            return fd

        real_close = os.close

        def fake_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        with (
            monkeypatch.context() as m,
            patch("os.fdopen", side_effect=OSError("injected")),
        ):
            m.setattr("os.open", fake_open)
            m.setattr("os.close", fake_close)
            with pytest.raises(OSError, match="injected"):
                write_proxy_config("ws://host:8420/mcp", "sk-test")

        assert captured_fds, "os.open must have been called"
        assert captured_fds[0] in closed_fds, "fd must be closed when os.fdopen raises"
        assert not tmp.exists(), ".tmp must be removed when os.fdopen raises"

    def test_tmp_removed_when_replace_raises(self, proxy_config_path: Path) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = proxy_config_path.with_suffix(".tmp")
        with (
            patch.object(Path, "replace", side_effect=OSError("rename failed")),
            pytest.raises(OSError, match="rename failed"),
        ):
            write_proxy_config("ws://host:8420/mcp", "sk-test")
        assert not tmp.exists(), ".tmp must be removed when replace() raises"


class TestDeleteProxyConfigFdLeak:
    """Verify delete_proxy_config() closes fd and removes .tmp on all failure paths."""

    def test_fd_closed_and_tmp_removed_when_fdopen_raises(
        self, proxy_config_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Write a config with an extra section so delete_proxy_config writes a .tmp.
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_config_path.write_text(
            '[quarry]\nurl = "ws://host:8420/mcp"\n\n[other]\nfoo = "bar"\n'
        )
        tmp = proxy_config_path.with_suffix(".tmp")
        captured_fds: list[int] = []
        closed_fds: list[int] = []

        real_open = os.open

        def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
            fd = real_open(path, flags, mode)
            captured_fds.append(fd)
            return fd

        real_close = os.close

        def fake_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        with (
            monkeypatch.context() as m,
            patch("os.fdopen", side_effect=OSError("injected")),
        ):
            m.setattr("os.open", fake_open)
            m.setattr("os.close", fake_close)
            with pytest.raises(OSError, match="injected"):
                delete_proxy_config()

        assert captured_fds, "os.open must have been called"
        assert captured_fds[0] in closed_fds, "fd must be closed when os.fdopen raises"
        assert not tmp.exists(), ".tmp must be removed when os.fdopen raises"

    def test_tmp_removed_when_replace_raises(self, proxy_config_path: Path) -> None:
        proxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_config_path.write_text(
            '[quarry]\nurl = "ws://host:8420/mcp"\n\n[other]\nfoo = "bar"\n'
        )
        tmp = proxy_config_path.with_suffix(".tmp")
        with (
            patch.object(Path, "replace", side_effect=OSError("rename failed")),
            pytest.raises(OSError, match="rename failed"),
        ):
            delete_proxy_config()
        assert not tmp.exists(), ".tmp must be removed when replace() raises"


class TestStoreCaCertFdLeak:
    """Verify store_ca_cert() closes fd and removes .tmp on all failure paths."""

    def test_fd_closed_and_tmp_removed_when_fdopen_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca_path = tmp_path / "quarry-ca.crt"
        monkeypatch.setattr("quarry.remote.CA_CERT_PATH", ca_path)
        tmp = ca_path.with_suffix(".tmp")
        captured_fds: list[int] = []
        closed_fds: list[int] = []

        real_open = os.open

        def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
            fd = real_open(path, flags, mode)
            captured_fds.append(fd)
            return fd

        real_close = os.close

        def fake_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        with (
            monkeypatch.context() as m,
            patch("os.fdopen", side_effect=OSError("injected")),
        ):
            m.setattr("os.open", fake_open)
            m.setattr("os.close", fake_close)
            with pytest.raises(OSError, match="injected"):
                store_ca_cert(pem)

        assert captured_fds, "os.open must have been called"
        assert captured_fds[0] in closed_fds, "fd must be closed when os.fdopen raises"
        assert not tmp.exists(), ".tmp must be removed when os.fdopen raises"


class TestValidateConnectionWithCaCert:
    def test_passes_ssl_context_when_ca_cert_path_provided(
        self, tmp_path: Path
    ) -> None:
        """When ca_cert_path is given and scheme=https, an SSLContext is created."""
        from quarry.tls import generate_ca

        # Write a real CA cert so ssl.create_default_context() can load it.
        ca_pem, _ = generate_ca("test.example.com")
        ca_file = tmp_path / "ca.crt"
        ca_file.write_bytes(ca_pem)

        ssl_contexts_used: list[object] = []

        def capture_urlopen(
            req: urllib.request.Request,
            timeout: int,
            context: object = None,
        ) -> MagicMock:
            ssl_contexts_used.append(context)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            validate_connection(
                "test.example.com",
                8420,
                "sk-test",
                scheme="https",
                ca_cert_path=str(ca_file),
            )

        assert len(ssl_contexts_used) == 1
        import ssl

        assert isinstance(ssl_contexts_used[0], ssl.SSLContext)

    def test_no_ssl_context_for_http(self) -> None:
        """When scheme=http, no SSLContext should be created."""
        ssl_contexts_used: list[object] = []

        def capture_urlopen(
            req: urllib.request.Request,
            timeout: int,
            context: object = None,
        ) -> MagicMock:
            ssl_contexts_used.append(context)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            validate_connection("localhost", 8420, "sk-test", scheme="http")

        assert len(ssl_contexts_used) == 1
        assert ssl_contexts_used[0] is None

    def test_missing_ca_file_returns_false_not_raises(self) -> None:
        """load_verify_locations() raising FileNotFoundError returns (False, ...)."""
        with patch(
            "ssl.SSLContext.load_verify_locations",
            side_effect=FileNotFoundError("no such file"),
        ):
            ok, reason = validate_connection(
                "localhost",
                8420,
                "sk-test",
                scheme="https",
                ca_cert_path="/nonexistent/ca.crt",
            )
        assert ok is False
        assert reason  # non-empty error message
