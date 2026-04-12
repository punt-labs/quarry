"""Tests for quarry.service — daemon lifecycle management."""

from __future__ import annotations

import os
import platform
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quarry.config import DEFAULT_PORT
from quarry.service import (
    _LABEL,
    _get_tls_hostname,
    _launchd_install,
    _launchd_plist_content,
    _quarry_exec_args,
    _systemd_escape,
    _systemd_install,
    _systemd_unit_content,
    _write_env_file,
    detect_platform,
    ensure_gpu_runtime,
    install,
    uninstall,
)

# Patch write_tls_files across all install() calls to avoid writing to
# ~/.punt-labs/quarry/tls/ during tests.
_PATCH_TLS = patch("quarry.service.write_tls_files")
_PATCH_CERT_FP = patch("quarry.service.cert_fingerprint", return_value="")


def _make_local_bin_quarry(home: Path) -> Path:
    """Create a fake ~/.local/bin/quarry under *home* for tests."""
    local_bin = home / ".local" / "bin" / "quarry"
    local_bin.parent.mkdir(parents=True, exist_ok=True)
    local_bin.write_text("#!/bin/sh\n")
    local_bin.chmod(0o755)
    return local_bin


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

    def test_raises_when_local_bin_missing(self, tmp_path: Path) -> None:
        """Raise RuntimeError when ~/.local/bin/quarry is absent."""
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
            pytest.raises(RuntimeError, match="Cannot find quarry binary"),
        ):
            _quarry_exec_args()

    def test_appends_tls_flag_when_cert_and_key_exist(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "server.crt").write_text("CERT")
        (tls_dir / "server.key").write_text("KEY")
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tls_dir),
        ):
            args = _quarry_exec_args()
        assert "--tls" in args

    def test_no_tls_flag_on_partial_state(self, tmp_path: Path) -> None:
        """Only server.crt present (no key) -> no --tls flag, warning logged."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "server.crt").write_text("CERT")
        _make_local_bin_quarry(tmp_path)
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
        _make_local_bin_quarry(tmp_path)
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
        _make_local_bin_quarry(tmp_path)
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
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--host" not in args

    def test_quarry_exec_args_never_contains_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QUARRY_API_KEY must never appear in exec args -- it stays in the env file."""
        monkeypatch.setenv("QUARRY_API_KEY", "testkey")
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--api-key" not in args
        assert "testkey" not in args

    def test_quarry_exec_args_no_api_key_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When QUARRY_API_KEY is absent, --api-key must not appear in args."""
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            args = _quarry_exec_args()
        assert "--api-key" not in args


class TestWriteEnvFile:
    def test_writes_api_key_double_quoted(self, tmp_path: Path) -> None:
        """_write_env_file writes QUARRY_API_KEY="<value>" double-quoted.

        Systemd EnvironmentFile treats # as an inline comment in unquoted values;
        double-quoting prevents silent truncation.
        """
        env_file = tmp_path / "quarry.env"
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file("s3cr3t")
        content = env_file.read_text()
        assert 'QUARRY_API_KEY="s3cr3t"' in content

    def test_key_containing_hash_not_truncated(self, tmp_path: Path) -> None:
        """A key containing # must be written in full, not truncated at the #.

        Unquoted, systemd treats # as a comment start — the stored key would be
        silently shortened and authentication would break.
        """
        env_file = tmp_path / "quarry.env"
        api_key = "abc#def"
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file(api_key)
        content = env_file.read_text()
        assert 'QUARRY_API_KEY="abc#def"' in content

    def test_key_containing_double_quote_escaped(self, tmp_path: Path) -> None:
        """A key containing " must have the character escaped, not left raw."""
        env_file = tmp_path / "quarry.env"
        api_key = 'tok"en'
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file(api_key)
        content = env_file.read_text()
        assert r'QUARRY_API_KEY="tok\"en"' in content

    def test_mode_0600(self, tmp_path: Path) -> None:
        """The env file must be created with mode 0600."""
        env_file = tmp_path / "quarry.env"
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file("k1")
        mode = stat.S_IMODE(env_file.stat().st_mode)
        assert mode == 0o600

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        """_write_env_file creates the parent directory if absent."""
        env_file = tmp_path / "nested" / "quarry.env"
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file("k1")
        assert env_file.exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        """_write_env_file replaces a pre-existing env file atomically."""
        env_file = tmp_path / "quarry.env"
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file("first")
            _write_env_file("second")
        content = env_file.read_text()
        assert '"second"' in content
        assert '"first"' not in content

    def test_fd_closed_and_tmp_removed_when_fdopen_raises(self, tmp_path: Path) -> None:
        """If os.fdopen raises, the raw fd must be closed and tmp file removed."""
        import os as _os

        env_file = tmp_path / "quarry.env"
        closed_fds: list[int] = []
        real_close = _os.close

        def fake_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        with (
            patch("quarry.service._ENV_FILE", env_file),
            patch("quarry.service.os.fdopen", side_effect=OSError("injected")),
            patch("quarry.service.os.close", side_effect=fake_close),
            pytest.raises(OSError, match="injected"),
        ):
            _write_env_file("k1")

        # No .tmp files should remain
        assert not any(tmp_path.glob("*.tmp"))
        # The raw fd must have been closed
        assert closed_fds, "os.close was never called after os.fdopen failure"


class TestServiceFileApiKey:
    def test_launchd_plist_contains_environment_variables_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_launchd_plist_content() embeds EnvironmentVariables and QUARRY_API_KEY."""
        monkeypatch.setenv("QUARRY_API_KEY", "k1")
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            content = _launchd_plist_content()
        assert "EnvironmentVariables" in content
        assert "QUARRY_API_KEY" in content
        assert "k1" in content
        # Must NOT appear in ProgramArguments args
        assert "--api-key" not in content

    def test_launchd_plist_no_env_vars_when_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When QUARRY_API_KEY is unset, no EnvironmentVariables block appears."""
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            content = _launchd_plist_content()
        assert "QUARRY_API_KEY" not in content
        assert "--api-key" not in content

    def test_launchd_plist_xml_escapes_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Characters &, <, > in QUARRY_API_KEY must be XML-escaped in the plist."""
        monkeypatch.setenv("QUARRY_API_KEY", "te&st<key>")
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            content = _launchd_plist_content()

        # Find just the EnvironmentVariables section to avoid false matches
        # in comments or other XML structure.
        assert "EnvironmentVariables" in content
        env_section_start = content.index("EnvironmentVariables")
        env_section = content[env_section_start:]

        # Escaped forms must appear in the EnvironmentVariables block.
        assert "&amp;" in env_section
        assert "&lt;" in env_section
        assert "&gt;" in env_section

        # Strip out all escaped sequences then verify no raw specials remain
        # in the EnvironmentVariables section.  Check only this section --
        # the full plist document uses & in the DTD URL (expected XML).
        stripped = (
            env_section.replace("&amp;", "")
            .replace("&lt;", "")
            .replace("&gt;", "")
            .replace("&quot;", "")
            .replace("&apos;", "")
        )
        assert "&" not in stripped
        assert "<key>" in content  # sanity: normal XML structure still valid
        # The raw un-escaped key value must not appear literally.
        assert "te&st<key>" not in content

    def test_plist_content_starts_with_xml_declaration_no_leading_whitespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plist must start with <?xml without leading whitespace."""
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            result = _launchd_plist_content()
        assert result.startswith("<?xml"), (
            f"Expected plist to start with <?xml, got: {result[:50]!r}"
        )

    def test_plist_content_with_api_key_no_leading_whitespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Embedding an API key must not break the outer textwrap.dedent."""
        monkeypatch.setenv("QUARRY_API_KEY", "test-key-abc")
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            result = _launchd_plist_content()
        assert result.startswith("<?xml"), (
            f"Expected <?xml at start, got: {result[:50]!r}"
        )
        assert "test-key-abc" in result
        assert "<key>EnvironmentVariables</key>" in result

    def test_systemd_unit_contains_environment_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_systemd_unit_content() must include EnvironmentFile= pointing to env."""
        monkeypatch.setenv("QUARRY_API_KEY", "k1")
        _make_local_bin_quarry(tmp_path)
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
        ):
            content = _systemd_unit_content()
        assert "EnvironmentFile=" in content
        assert "quarry.env" in content
        # Must NOT pass the key as a CLI arg
        assert "--api-key" not in content


class TestLaunchdPlistArgEncoding:
    def test_path_with_spaces_not_shell_quoted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ProgramArguments strings must be XML-escaped, not shell-quoted.

        shlex.quote() would wrap '/path with spaces/quarry' in single-quotes,
        producing the literal string `'/path with spaces/quarry'` (including
        quote chars) as the exec argument — breaking execution.  launchd passes
        ProgramArguments directly to exec; there is no shell.
        """
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        spacey_path = "/path with spaces/quarry"
        with (
            patch(
                "quarry.service._quarry_exec_args",
                return_value=[spacey_path, "serve"],
            ),
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service.TLS_DIR", tmp_path / "tls"),
        ):
            content = _launchd_plist_content()

        # The path must appear verbatim inside <string> tags.
        assert f"<string>{spacey_path}</string>" in content
        # shlex.quote wraps in single-quotes — that must NOT happen.
        assert f"<string>'{spacey_path}'</string>" not in content


class TestInstallHostKeyGuard:
    """install() must reject non-loopback QUARRY_SERVE_HOST with no API key."""

    @patch("quarry.service.write_tls_files")
    @patch("quarry.service.cert_fingerprint", return_value="")
    def test_raises_when_non_loopback_host_and_no_api_key(
        self,
        _fp: MagicMock,
        _tls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """install() raises SystemExit when QUARRY_SERVE_HOST=0.0.0.0 and no API key."""
        monkeypatch.setenv("QUARRY_SERVE_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        with pytest.raises(SystemExit, match="QUARRY_API_KEY is empty"):
            install()

    @patch("quarry.service.write_tls_files")
    @patch("quarry.service.cert_fingerprint", return_value="")
    def test_raises_when_custom_host_and_empty_api_key(
        self,
        _fp: MagicMock,
        _tls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """install() raises SystemExit for any non-loopback host with empty API key."""
        monkeypatch.setenv("QUARRY_SERVE_HOST", "192.168.1.50")
        monkeypatch.setenv("QUARRY_API_KEY", "")
        with pytest.raises(SystemExit, match="QUARRY_API_KEY is empty"):
            install()

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    @patch("quarry.service.write_tls_files")
    @patch("quarry.service.cert_fingerprint", return_value="")
    def test_loopback_host_without_api_key_succeeds(
        self,
        _fp: MagicMock,
        _tls: MagicMock,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """install() with QUARRY_SERVE_HOST=127.0.0.1 and no key must not raise."""
        monkeypatch.setenv("QUARRY_SERVE_HOST", "127.0.0.1")
        monkeypatch.delenv("QUARRY_API_KEY", raising=False)
        _make_local_bin_quarry(tmp_path)
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=113),  # launchctl list: not found
                MagicMock(returncode=0),  # launchctl load: success
                MagicMock(returncode=0),  # launchctl list: running
            ]
            msg = install()
        assert "running" in msg

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    @patch("quarry.service.write_tls_files")
    @patch("quarry.service.cert_fingerprint", return_value="")
    def test_non_loopback_host_with_api_key_succeeds(
        self,
        _fp: MagicMock,
        _tls: MagicMock,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """install() with non-loopback host AND a valid API key must not raise."""
        monkeypatch.setenv("QUARRY_SERVE_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.setenv("QUARRY_API_KEY", "validkey")
        _make_local_bin_quarry(tmp_path)
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=113),  # launchctl list: not found
                MagicMock(returncode=0),  # launchctl load: success
                MagicMock(returncode=0),  # launchctl list: running
            ]
            msg = install()
        assert "running" in msg


class TestInstallMacOS:
    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    def test_fresh_install_writes_plist_and_loads(
        self,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        _make_local_bin_quarry(tmp_path)
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            # First call: launchctl list -> not found (fresh install)
            # Second call: launchctl load -> success
            # Third call: launchctl list -> running
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
        self,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        _make_local_bin_quarry(tmp_path)
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        plist_path.write_text("<plist>old binary</plist>")
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            # First call: launchctl list -> found (existing service)
            # Second call: launchctl unload -> success
            # Third call: launchctl load -> success
            # Fourth call: launchctl list -> running
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

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    def test_plist_written_with_mode_0600(
        self,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """_launchd_install() must chmod the plist to 0600.

        The plist embeds QUARRY_API_KEY so it must be owner-read-only.
        """
        _make_local_bin_quarry(tmp_path)
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
            _PATCH_TLS,
            _PATCH_CERT_FP,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=113),  # launchctl list: not found (fresh install)
                MagicMock(returncode=0),  # launchctl load: success
                MagicMock(returncode=0),  # launchctl list: running
            ]
            install()

        assert plist_path.exists()
        mode = stat.S_IMODE(plist_path.stat().st_mode)
        assert oct(mode) == oct(0o600), f"expected 0o600, got {oct(mode)}"


class TestInstallLinux:
    @patch("quarry.service._has_linger", return_value=True)
    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Linux")
    def test_writes_unit_and_enables(
        self,
        _sys: MagicMock,
        mock_run: MagicMock,
        _linger: MagicMock,
        tmp_path: Path,
    ) -> None:
        _make_local_bin_quarry(tmp_path)
        unit_path = tmp_path / "quarry.service"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
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

            # Verify daemon-reload, enable, restart, and is-active calls
            assert mock_run.call_count >= 4  # daemon-reload, enable, restart, is-active
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert any("daemon-reload" in c for c in calls)
            assert any("enable" in c for c in calls)
            assert any("restart" in c for c in calls)

    @patch("quarry.service._has_linger", return_value=False)
    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Linux")
    def test_warns_without_linger(
        self,
        _sys: MagicMock,
        mock_run: MagicMock,
        _linger: MagicMock,
        tmp_path: Path,
    ) -> None:
        _make_local_bin_quarry(tmp_path)
        unit_path = tmp_path / "quarry.service"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
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


class TestSystemdInstallCallsRestart:
    """_systemd_install() must call systemctl restart after enable --now."""

    @patch("quarry.service.subprocess.run")
    def test_restart_called_after_enable(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        unit_path = tmp_path / "quarry.service"
        with (
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
            patch(
                "quarry.service._quarry_exec_args",
                return_value=["quarry", "serve", "--port", "8420"],
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _systemd_install()

        calls = [c.args[0] for c in mock_run.call_args_list]
        enable_call = ["systemctl", "--user", "enable", "--now", "quarry"]
        restart_call = ["systemctl", "--user", "restart", "quarry"]
        assert enable_call in calls
        assert restart_call in calls
        enable_idx = calls.index(enable_call)
        restart_idx = calls.index(restart_call)
        assert restart_idx > enable_idx, "restart must come after enable --now"


class TestLaunchdPlistAtomicWrite:
    """Verify _launchd_install() writes the plist atomically with mode 0600."""

    @patch("quarry.service.subprocess.run")
    def test_plist_created_with_mode_0600(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """The plist file must be created with mode 0600 (no world-readable window)."""
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        mock_run.side_effect = [
            MagicMock(returncode=113),  # launchctl list: not found
            MagicMock(returncode=0),  # launchctl load: success
        ]
        with (
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._quarry_exec_args", return_value=["quarry", "serve"]),
        ):
            _launchd_install()

        assert plist_path.exists()
        mode = stat.S_IMODE(plist_path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    @patch("quarry.service.subprocess.run")
    def test_fd_closed_and_tmp_removed_when_fdopen_raises(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """If os.fdopen raises during plist write, the raw fd is closed and tmp removed.

        Fix 3 invariant: no temp file leaks and no fd leaks on fdopen failure.
        """
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        closed_fds: list[int] = []
        real_close = os.close

        def fake_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        mock_run.return_value = MagicMock(returncode=113)  # launchctl list: not found

        with (
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._quarry_exec_args", return_value=["quarry", "serve"]),
            patch("quarry.service.os.fdopen", side_effect=OSError("injected")),
            patch("quarry.service.os.close", side_effect=fake_close),
            pytest.raises(OSError, match="injected"),
        ):
            _launchd_install()

        # No .tmp files should remain
        assert not any(tmp_path.glob("*.tmp"))
        # The raw fd must have been closed
        assert closed_fds, "os.close was never called after os.fdopen failure"


class TestWriteEnvFileBackslash:
    """Verify _write_env_file correctly escapes backslashes in API keys."""

    def test_backslash_escaped(self, tmp_path: Path) -> None:
        """A backslash in the API key must be doubled in the env file."""
        env_file = tmp_path / "quarry.env"
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file("tok\\en")
        content = env_file.read_text()
        assert 'QUARRY_API_KEY="tok\\\\en"' in content

    def test_backslash_and_quote_escaped(self, tmp_path: Path) -> None:
        """Keys with both backslashes and double-quotes must have both escaped."""
        env_file = tmp_path / "quarry.env"
        api_key = 'value\\with\\backslashes"and"quotes'
        with patch("quarry.service._ENV_FILE", env_file):
            _write_env_file(api_key)
        content = env_file.read_text()
        expected = r'QUARRY_API_KEY="value\\with\\backslashes\"and\"quotes"'
        assert expected in content


class TestSystemdEscape:
    """Unit tests for _systemd_escape() — systemd ExecStart argument escaper.

    systemd uses its own parser, not POSIX shell.  shlex.quote() produces
    single-quote style escaping (e.g. 'foo'"'"'bar') that systemd cannot
    decode.  _systemd_escape() must always use double-quote style.
    """

    def test_plain_arg_double_quoted(self) -> None:
        """A plain argument must be wrapped in double quotes."""
        assert _systemd_escape("quarry") == '"quarry"'

    def test_path_with_spaces_double_quoted(self) -> None:
        """A path with spaces must be double-quoted, not single-quoted."""
        result = _systemd_escape("/home/user/my bin/quarry")
        assert result == '"/home/user/my bin/quarry"'
        assert "'" not in result

    def test_single_quote_in_path_no_posix_escape(self) -> None:
        """A path with a single quote must NOT produce POSIX shell escaping.

        shlex.quote would produce the POSIX escape sequence (e.g. 'x'"'"'y')
        which systemd cannot parse.  _systemd_escape() must use double-quote style,
        leaving single quotes bare inside the outer double quotes.
        """
        path = "/home/o'brien/.local/bin/quarry"
        result = _systemd_escape(path)
        # The full POSIX single-quote escape sequence '"'"' must not appear.
        posix_escape = "'" + '"' + "'" + '"' + "'"
        assert posix_escape not in result, f"POSIX escape pattern found in: {result!r}"
        # Single quotes inside double-quoted strings are fine in systemd — they
        # must appear verbatim, not escaped.
        assert "o'brien" in result
        # Must use double-quote wrapping.
        assert result.startswith('"')
        assert result.endswith('"')

    def test_embedded_double_quote_backslash_escaped(self) -> None:
        """An embedded double-quote must be backslash-escaped."""
        result = _systemd_escape('say "hello"')
        assert result == '"say \\"hello\\""'

    def test_backslash_doubled(self) -> None:
        """A backslash in the argument must be doubled."""
        result = _systemd_escape("C:\\path\\to\\quarry")
        assert result == '"C:\\\\path\\\\to\\\\quarry"'

    def test_systemd_unit_exec_start_no_posix_escape(self, tmp_path: Path) -> None:
        """_systemd_unit_content() ExecStart must not contain the POSIX escape pattern.

        When the quarry binary path contains a single quote, shlex.quote()
        would produce 'foo'"'"'bar' which systemd rejects.
        """
        single_quote_path = "/home/o'brien/.local/bin/quarry"
        with (
            patch(
                "quarry.service._quarry_exec_args",
                return_value=[single_quote_path, "serve", "--port", "8420"],
            ),
            patch("quarry.service._ENV_FILE", tmp_path / "quarry.env"),
        ):
            content = _systemd_unit_content()

        exec_start_line = next(
            line for line in content.splitlines() if line.startswith("ExecStart=")
        )
        # The full POSIX single-quote escape sequence '"'"' must not appear.
        # shlex.quote produces this pattern; systemd cannot parse it.
        assert "'" + '"' + "'" + '"' + "'" not in exec_start_line, (
            f"ExecStart must not use POSIX shell escaping; got: {exec_start_line!r}"
        )
        # The path must appear with the single-quote verbatim inside double quotes.
        assert '"' + "/home/o'brien/.local/bin/quarry" + '"' in exec_start_line


class TestInstallMacOSSkipsEnvFile:
    """install() on macOS must NOT write quarry.env even when QUARRY_API_KEY is set.

    macOS launchd reads the key from the plist EnvironmentVariables block.
    Writing it to an env file is unnecessary and duplicates the secret on disk.
    """

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Darwin")
    @patch("quarry.service.write_tls_files")
    @patch("quarry.service.cert_fingerprint", return_value="")
    def test_no_env_file_written_on_macos(
        self,
        _fp: MagicMock,
        _tls: MagicMock,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("QUARRY_API_KEY", "sekrit")
        _make_local_bin_quarry(tmp_path)
        plist_path = tmp_path / "com.punt-labs.quarry.plist"
        env_file = tmp_path / "quarry.env"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._LAUNCHD_DIR", tmp_path),
            patch("quarry.service._LAUNCHD_PLIST", plist_path),
            patch("quarry.service._ENV_FILE", env_file),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=113),  # launchctl list: not found
                MagicMock(returncode=0),  # launchctl load: success
                MagicMock(returncode=0),  # launchctl list: running
            ]
            install()

        assert not env_file.exists(), (
            "install() must not write quarry.env on macOS -- "
            "the API key goes into the plist EnvironmentVariables block"
        )

    @patch("quarry.service.subprocess.run")
    @patch.object(platform, "system", return_value="Linux")
    @patch("quarry.service._has_linger", return_value=True)
    @patch("quarry.service.write_tls_files")
    @patch("quarry.service.cert_fingerprint", return_value="")
    def test_env_file_written_on_linux(
        self,
        _fp: MagicMock,
        _tls: MagicMock,
        _linger: MagicMock,
        _sys: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the complementary path: Linux does write the env file."""
        monkeypatch.setenv("QUARRY_API_KEY", "sekrit")
        _make_local_bin_quarry(tmp_path)
        unit_path = tmp_path / "quarry.service"
        env_file = tmp_path / "quarry.env"
        with (
            patch("quarry.service.Path.home", return_value=tmp_path),
            patch("quarry.service._SYSTEMD_DIR", tmp_path),
            patch("quarry.service._SYSTEMD_UNIT", unit_path),
            patch("quarry.service._ENV_FILE", env_file),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n")
            install()

        assert env_file.exists(), (
            "install() must write quarry.env on Linux so systemd EnvironmentFile= works"
        )
        content = env_file.read_text()
        assert "QUARRY_API_KEY" in content


class TestEnsureGpuRuntime:
    """Tests for ensure_gpu_runtime() — NVIDIA GPU detection and onnxruntime swap."""

    def test_no_uv_on_path(self) -> None:
        """When uv is not on PATH, return early without any subprocess calls."""
        with patch("quarry.service.shutil.which", return_value=None):
            result = ensure_gpu_runtime()
        assert result == "uv not found, skipped GPU check"

    def test_no_nvidia_smi(self) -> None:
        """When nvidia-smi is absent, return 'no NVIDIA GPU'."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return None
            return None

        with patch("quarry.service.shutil.which", side_effect=which_side_effect):
            result = ensure_gpu_runtime()
        assert result == "no NVIDIA GPU"

    def test_nvidia_smi_fails(self) -> None:
        """When nvidia-smi exists but fails, return 'no NVIDIA GPU'."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        with (
            patch("quarry.service.shutil.which", side_effect=which_side_effect),
            patch(
                "quarry.service.subprocess.run",
                return_value=MagicMock(returncode=1),
            ),
        ):
            result = ensure_gpu_runtime()
        assert result == "no NVIDIA GPU"

    def test_cuda_already_available(self) -> None:
        """When CUDAExecutionProvider is already available, return early."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if cmd[0] == "/usr/bin/nvidia-smi":
                return MagicMock(returncode=0)
            # Provider check subprocess — report CUDA available.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CUDAExecutionProvider,CPUExecutionProvider\n",
                )
            return MagicMock(returncode=0)

        with (
            patch("quarry.service.shutil.which", side_effect=which_side_effect),
            patch("quarry.service.subprocess.run", side_effect=run_side_effect),
        ):
            result = ensure_gpu_runtime()

        assert result == "CUDA already available"
        # nvidia-smi + provider check = 2 subprocess calls, no pip commands.
        assert len(calls) == 2

    def test_swap_success(self) -> None:
        """When nvidia-smi works and CUDA not available, swap succeeds."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        call_count = 0

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # Provider check subprocess — report CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            return MagicMock(returncode=0)

        with (
            patch("quarry.service.shutil.which", side_effect=which_side_effect),
            patch(
                "quarry.service.subprocess.run",
                side_effect=run_side_effect,
            ),
        ):
            result = ensure_gpu_runtime()

        assert result == "onnxruntime-gpu installed"
        # nvidia-smi + provider check + uninstall + install = 4 subprocess calls
        assert call_count == 4

    def test_swap_failure_restores_cpu(self) -> None:
        """When onnxruntime-gpu install fails, CPU onnxruntime is restored."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(cmd)
            # Provider check — CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            # nvidia-smi OK, uninstall OK, gpu fails, cpu restore OK
            if "onnxruntime-gpu>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with (
            patch("quarry.service.shutil.which", side_effect=which_side_effect),
            patch("quarry.service.subprocess.run", side_effect=run_side_effect),
        ):
            result = ensure_gpu_runtime()

        assert result == "onnxruntime-gpu install failed, CPU restored"
        # Verify CPU restore was called
        restore_calls = [c for c in calls if "onnxruntime>=1.18.0" in c]
        assert len(restore_calls) == 1
        # Return value distinguishes from the "restore also failed" case.
        assert "also failed" not in result

    def test_swap_failure_restore_also_fails(self) -> None:
        """When both GPU install and CPU restore fail, return a distinct message."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            # Provider check — CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            # nvidia-smi OK, uninstall OK, gpu install fails, cpu restore fails
            if "onnxruntime-gpu>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            if "onnxruntime>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with (
            patch("quarry.service.shutil.which", side_effect=which_side_effect),
            patch("quarry.service.subprocess.run", side_effect=run_side_effect),
        ):
            result = ensure_gpu_runtime()

        assert result == "onnxruntime-gpu install failed, CPU restore also failed"

    def test_swap_success_clears_module_cache(self) -> None:
        """After a successful swap, 'onnxruntime' must not remain in sys.modules."""
        import sys as _sys

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        mock_ort = MagicMock()

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            # Provider check subprocess — report CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            return MagicMock(returncode=0)

        with (
            patch("quarry.service.shutil.which", side_effect=which_side_effect),
            patch(
                "quarry.service.subprocess.run",
                side_effect=run_side_effect,
            ),
            patch.dict("sys.modules", {"onnxruntime": mock_ort}),
        ):
            result = ensure_gpu_runtime()
            # Assert inside the patch.dict context — on exit it restores
            # the original sys.modules state, which would re-add the key.
            assert "onnxruntime" not in _sys.modules

        assert result == "onnxruntime-gpu installed"
