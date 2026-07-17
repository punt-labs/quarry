"""Tests for DaemonDiagnostics — quarryd reachability and serve.token checks.

These close the "doctor can't diagnose the thing it tells you to run doctor for"
gap: fail-closed loopback auth points operators at ``quarry doctor``, so doctor
must actually report a token/daemon outage.  No real daemon — the /health probe
and the run dir are mocked.
"""

from __future__ import annotations

import ssl
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from quarry.doctor_daemon import DaemonDiagnostics


@contextmanager
def _run_dir_at(tmp_path: Path) -> Generator[None]:
    """Patch Settings so the active-db run dir resolves to ``tmp_path``."""
    fake_settings = MagicMock()
    fake_settings.lancedb_path = tmp_path / "lancedb"  # parent == tmp_path
    with patch("quarry.doctor_daemon.Settings") as settings:
        settings.load.return_value.resolve_db_paths.return_value = fake_settings
        settings.active_db.return_value = None
        yield


class TestReachability:
    def test_no_port_file_reports_not_reachable(self, tmp_path: Path) -> None:
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.reachability()
        assert result.passed is False
        assert result.required is False  # advisory — a down daemon is not a crash
        assert "not reachable" in result.message

    def test_port_present_and_health_ready_passes(self, tmp_path: Path) -> None:
        (tmp_path / "serve.port").write_text("8420")
        with (
            _run_dir_at(tmp_path),
            patch.object(DaemonDiagnostics, "_probe_health", return_value=True),
        ):
            result = DaemonDiagnostics.reachability()
        assert result.passed is True
        assert "ready" in result.message
        assert "127.0.0.1:8420" in result.message

    def test_port_present_but_not_ready_fails(self, tmp_path: Path) -> None:
        (tmp_path / "serve.port").write_text("8420")
        with (
            _run_dir_at(tmp_path),
            patch.object(DaemonDiagnostics, "_probe_health", return_value=False),
        ):
            result = DaemonDiagnostics.reachability()
        assert result.passed is False
        assert "not ready" in result.message

    def test_corrupt_port_file_reports_not_reachable(self, tmp_path: Path) -> None:
        # A non-numeric serve.port raises ValueError in PortFile.read — fail soft.
        (tmp_path / "serve.port").write_text("not-a-port")
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.reachability()
        assert result.passed is False
        assert "not reachable" in result.message

    def test_probe_never_targets_a_name(self, tmp_path: Path) -> None:
        # The probe pins the literal 127.0.0.1, never the ambiguous "localhost".
        (tmp_path / "serve.port").write_text("8420")
        captured: dict[str, object] = {}

        def _fake_conn(host: str, port: int, **_kwargs: object) -> MagicMock:
            captured["host"] = host
            conn = MagicMock()
            conn.getresponse.return_value.status = 200
            conn.getresponse.return_value.read.return_value = b'{"state":"ready"}'
            return conn

        with (
            _run_dir_at(tmp_path),
            patch("quarry.doctor_daemon.http.client.HTTPSConnection", _fake_conn),
        ):
            result = DaemonDiagnostics.reachability()
        assert captured["host"] == "127.0.0.1"
        assert result.passed is True


class TestServeToken:
    def test_missing_token_fails(self, tmp_path: Path) -> None:
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.serve_token()
        assert result.passed is False
        assert result.required is False
        assert "missing or unreadable" in result.message

    def test_present_0600_nonempty_passes(self, tmp_path: Path) -> None:
        token = tmp_path / "serve.token"
        token.write_text("live-token")
        token.chmod(0o600)
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.serve_token()
        assert result.passed is True
        assert "0600" in result.message

    def test_wrong_mode_fails(self, tmp_path: Path) -> None:
        token = tmp_path / "serve.token"
        token.write_text("live-token")
        token.chmod(0o644)  # world-readable — not the 0600 the writer guarantees
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.serve_token()
        assert result.passed is False
        assert "mode" in result.message and "0600" in result.message

    def test_empty_token_fails(self, tmp_path: Path) -> None:
        token = tmp_path / "serve.token"
        token.write_text("   ")  # whitespace-only == empty credential
        token.chmod(0o600)
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.serve_token()
        assert result.passed is False
        assert "empty" in result.message


class TestHealthParsing:
    def test_ready_body_is_ready(self) -> None:
        assert DaemonDiagnostics._is_ready(b'{"state": "ready", "uptime": 1}') is True

    def test_starting_body_is_not_ready(self) -> None:
        assert DaemonDiagnostics._is_ready(b'{"state":"starting"}') is False

    def test_non_json_body_is_not_ready(self) -> None:
        assert DaemonDiagnostics._is_ready(b"not json at all") is False

    def test_non_object_body_is_not_ready(self) -> None:
        assert DaemonDiagnostics._is_ready(b'["ready"]') is False


class TestProbeFailSoft:
    def test_connection_error_is_not_ready(self, tmp_path: Path) -> None:
        # A refused connection (daemon down) must be a False result, not a raise.
        def _boom(*_args: object, **_kwargs: object) -> MagicMock:
            conn = MagicMock()
            conn.request.side_effect = OSError("connection refused")
            return conn

        with patch("quarry.doctor_daemon.http.client.HTTPSConnection", _boom):
            assert DaemonDiagnostics._probe_health(8420) is False

    def test_non_200_status_is_not_ready(self) -> None:
        def _conn(*_args: object, **_kwargs: object) -> MagicMock:
            conn = MagicMock()
            conn.getresponse.return_value.status = 503
            return conn

        with patch("quarry.doctor_daemon.http.client.HTTPSConnection", _conn):
            assert DaemonDiagnostics._probe_health(8420) is False


class TestProbeTransportFallback:
    """A TLS daemon is probed over HTTPS; a plaintext daemon over HTTP."""

    @staticmethod
    def _ready_conn(*_args: object, **_kwargs: object) -> MagicMock:
        conn = MagicMock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"state":"ready"}'
        return conn

    def test_tls_daemon_ready_over_https(self) -> None:
        with patch(
            "quarry.doctor_daemon.http.client.HTTPSConnection", self._ready_conn
        ):
            assert DaemonDiagnostics._probe_health(8420) is True

    def test_plaintext_daemon_ready_via_http_fallback(self) -> None:
        # HTTPS raises SSLError (plaintext behind https / wrong-version-number);
        # the probe must retry over plain HTTP and report the daemon ready.
        def _https_ssl_error(*_args: object, **_kwargs: object) -> MagicMock:
            conn = MagicMock()
            conn.request.side_effect = ssl.SSLError("WRONG_VERSION_NUMBER")
            return conn

        with (
            patch("quarry.doctor_daemon.http.client.HTTPSConnection", _https_ssl_error),
            patch("quarry.doctor_daemon.http.client.HTTPConnection", self._ready_conn),
        ):
            assert DaemonDiagnostics._probe_health(8420) is True

    def test_down_daemon_not_ready_no_raise(self) -> None:
        # A refused connection (not a TLS error) is a not-ready result, never a
        # raise and never an HTTP retry (the failure is not a plaintext-behind-TLS).
        def _refused(*_args: object, **_kwargs: object) -> MagicMock:
            conn = MagicMock()
            conn.request.side_effect = ConnectionRefusedError("refused")
            return conn

        with patch("quarry.doctor_daemon.http.client.HTTPSConnection", _refused):
            assert DaemonDiagnostics._probe_health(8420) is False
