"""Tests for DaemonDiagnostics — quarryd reachability and serve.token checks.

These close the "doctor can't diagnose the thing it tells you to run doctor for"
gap: fail-closed loopback auth points operators at ``quarry doctor``, so doctor
must actually report a token/daemon outage.  No real daemon — the /health probe
and the run dir are mocked.
"""

from __future__ import annotations

import logging
import resource
import socket
import ssl
import struct
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PosixPath
from unittest.mock import MagicMock, patch

import pytest

from quarry.api.meta import FdHealth
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.fd_headroom import FdHeadroom
from quarry.tls import generate_ca, generate_server_cert


@contextmanager
def _run_dir_at(tmp_path: Path) -> Generator[None]:
    """Patch Settings so the active-db run dir resolves to ``tmp_path``."""
    fake_settings = MagicMock()
    fake_settings.lancedb_path = tmp_path / "lancedb"  # parent == tmp_path
    with patch("quarry.doctor_daemon.Settings") as settings:
        settings.load.return_value.resolve_db_paths.return_value = fake_settings
        yield


@contextmanager
def _ca_at(tmp_path: Path, content: bytes) -> Generator[None]:
    """Make ``tmp_path/ca.crt`` (holding *content*) the daemon's installed CA."""
    ca = tmp_path / "ca.crt"
    ca.write_bytes(content)
    with patch("quarry.home_paths.QuarryHome.daemon_ca", return_value=ca):
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


class _DeniedPath(PosixPath):
    """A path whose existence cannot even be asked about."""

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        del follow_symlinks
        raise PermissionError(13, "Permission denied")


class TestPinnedCa:
    """An installed CA that cannot be used is a failed check, never CERT_NONE."""

    def test_a_corrupt_ca_fails_reachability_naming_the_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "serve.port").write_text("8420")
        with _run_dir_at(tmp_path), _ca_at(tmp_path, b"not a certificate"):
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert str(tmp_path / "ca.crt") in result.message
        assert "not reachable" not in result.message

    def test_an_unreadable_ca_fails_reachability_with_the_errno(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "serve.port").write_text("8420")
        ca = tmp_path / "ca.crt"
        ca.write_bytes(b"x")
        denied = PermissionError(13, "Permission denied")
        with (
            _run_dir_at(tmp_path),
            patch("quarry.home_paths.QuarryHome.daemon_ca", return_value=ca),
            patch(
                "quarry.doctor_daemon.ssl.SSLContext.load_verify_locations",
                side_effect=denied,
            ),
        ):
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert "ca.crt" in result.message
        assert "EACCES" in result.message

    def test_a_corrupt_ca_fails_the_fd_check_too(self, tmp_path: Path) -> None:
        (tmp_path / "serve.port").write_text("8420")
        with _run_dir_at(tmp_path), _ca_at(tmp_path, b"junk"):
            result = DaemonDiagnostics.fd_headroom()

        assert result.passed is False
        assert "ca.crt" in result.message

    def test_no_ca_at_all_still_probes_unverified(self, tmp_path: Path) -> None:
        missing = tmp_path / "ca.crt"
        with patch("quarry.home_paths.QuarryHome.daemon_ca", return_value=missing):
            pinned = DaemonDiagnostics._pinned_context()

        assert pinned is None
        assert DaemonDiagnostics._unverified_context().verify_mode == ssl.CERT_NONE

    def test_a_ca_that_cannot_even_be_looked_up_names_the_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "serve.port").write_text("8420")
        ca = _DeniedPath(tmp_path / "ca.crt")
        with (
            _run_dir_at(tmp_path),
            patch("quarry.home_paths.QuarryHome.daemon_ca", return_value=ca),
        ):
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert f"{ca} cannot be read (EACCES)" in result.message


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
    def test_object_body_parses(self) -> None:
        assert DaemonDiagnostics._parse_object(b'{"state": "ready", "uptime": 1}') == {
            "state": "ready",
            "uptime": 1,
        }

    def test_non_json_body_is_none(self) -> None:
        assert DaemonDiagnostics._parse_object(b"not json at all") is None

    def test_non_object_body_is_none(self) -> None:
        assert DaemonDiagnostics._parse_object(b'["ready"]') is None


class TestFdHeadroom:
    """The FD-headroom check reports the DAEMON's headroom, read from /health."""

    @staticmethod
    def _ready_fd(open_fds: int, soft_limit: int) -> dict[str, object]:
        return {
            "state": "ready",
            "fd": {"open_fds": open_fds, "soft_limit": soft_limit},
        }

    def test_reports_daemon_soft_limit_not_local_ulimit(self, tmp_path: Path) -> None:
        # The daemon reports soft_limit 8192; the local process ulimit is
        # whatever this test's shell set. A pass proves the number came from the
        # daemon's /health, not a local getrlimit sample (the observability bug).
        (tmp_path / "serve.port").write_text("8420")
        local_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        with (
            _run_dir_at(tmp_path),
            patch.object(
                DaemonDiagnostics,
                "_fetch_health",
                return_value=self._ready_fd(100, 8192),
            ),
        ):
            result = DaemonDiagnostics.fd_headroom()
        assert result.passed is True
        assert "100/8192 fds" in result.message  # the DAEMON's soft limit drives it
        if local_soft != 8192:
            assert f"/{local_soft} fds" not in result.message

    def test_never_samples_the_local_process(self, tmp_path: Path) -> None:
        # A daemon-sourced check must NEVER touch the local sampler; a poisoned
        # FdHeadroom.sample would blow up if the check fell back to it.
        (tmp_path / "serve.port").write_text("8420")
        with (
            _run_dir_at(tmp_path),
            patch.object(
                FdHeadroom,
                "sample",
                side_effect=AssertionError("fd_headroom sampled the local process"),
            ),
            patch.object(
                DaemonDiagnostics,
                "_fetch_health",
                return_value=self._ready_fd(100, 8192),
            ),
        ):
            result = DaemonDiagnostics.fd_headroom()
        assert result.passed is True

    def test_unreachable_daemon_reports_unavailable(self, tmp_path: Path) -> None:
        # No serve.port → daemon not running: degrade clearly, no local fallback.
        with _run_dir_at(tmp_path):
            result = DaemonDiagnostics.fd_headroom()
        assert result.passed is False
        assert "unavailable" in result.message
        assert "not running" in result.message

    def test_health_unreadable_reports_unavailable(self, tmp_path: Path) -> None:
        (tmp_path / "serve.port").write_text("8420")
        with (
            _run_dir_at(tmp_path),
            patch.object(DaemonDiagnostics, "_fetch_health", return_value=None),
        ):
            result = DaemonDiagnostics.fd_headroom()
        assert result.passed is False
        assert "unavailable" in result.message

    def test_daemon_reported_null_fd_degrades(self, tmp_path: Path) -> None:
        # Daemon reachable but could not sample its own fds (fd: null).
        (tmp_path / "serve.port").write_text("8420")
        body: dict[str, object] = {"state": "ready", "fd": None}
        with (
            _run_dir_at(tmp_path),
            patch.object(DaemonDiagnostics, "_fetch_health", return_value=body),
        ):
            result = DaemonDiagnostics.fd_headroom()
        assert result.passed is False
        assert "no fd headroom" in result.message

    def test_low_headroom_warns(self, tmp_path: Path) -> None:
        (tmp_path / "serve.port").write_text("8420")
        with (
            _run_dir_at(tmp_path),
            patch.object(
                DaemonDiagnostics,
                "_fetch_health",
                return_value=self._ready_fd(250, 256),
            ),
        ):
            result = DaemonDiagnostics.fd_headroom()
        assert result.passed is False
        assert "over 80%" in result.message


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


class TestFdWireContract:
    """FdHealth is the single owner of the fd wire shape.

    The daemon serializes an ``FdHealth``; doctor parses the same model back, so
    a field added to the contract cannot drift between the two hand-sites.
    """

    def test_producer_shape_round_trips_through_doctor(self) -> None:
        # Exactly what the /health route emits: FdHealth(...).model_dump().
        wire = FdHealth(open_fds=100, soft_limit=8192).model_dump()
        headroom = DaemonDiagnostics._fd_from_health({"fd": wire})
        assert headroom == FdHeadroom(open_fds=100, soft_limit=8192)

    def test_null_fd_degrades_to_none(self) -> None:
        assert DaemonDiagnostics._fd_from_health({"fd": None}) is None

    def test_missing_fd_key_degrades_to_none(self) -> None:
        assert DaemonDiagnostics._fd_from_health({"state": "ready"}) is None

    def test_non_object_fd_degrades_to_none(self) -> None:
        # A string where the object is expected fails validation → None, no raise.
        assert DaemonDiagnostics._fd_from_health({"fd": "nope"}) is None

    def test_nonpositive_soft_limit_rejected(self) -> None:
        # gt=0 on FdHealth.soft_limit stops a zero/negative limit from
        # deserializing and reaching FdHeadroom.utilization's divide.
        zero = DaemonDiagnostics._fd_from_health(
            {"fd": {"open_fds": 1, "soft_limit": 0}}
        )
        assert zero is None
        neg = DaemonDiagnostics._fd_from_health(
            {"fd": {"open_fds": 1, "soft_limit": -5}}
        )
        assert neg is None

    def test_negative_open_fds_rejected(self) -> None:
        # ge=0 on FdHealth.open_fds rejects a negative count on the wire.
        result = DaemonDiagnostics._fd_from_health(
            {"fd": {"open_fds": -1, "soft_limit": 8192}}
        )
        assert result is None


class _Health(BaseHTTPRequestHandler):
    """Answer ``/health`` with a ready daemon, quietly."""

    def do_GET(self) -> None:
        body = b'{"state":"ready"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _health_server(tls: tuple[bytes, bytes] | None, root: Path) -> Generator[int]:
    """Serve ``/health`` on the loopback, over TLS with *tls* (cert, key) if given."""
    server = HTTPServer(("127.0.0.1", 0), _Health)
    if tls is not None:
        cert, key = root / "server.crt", root / "server.key"
        cert.write_bytes(tls[0])
        key.write_bytes(tls[1])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _signed_by(ca: tuple[bytes, bytes]) -> tuple[bytes, bytes]:
    return generate_server_cert(ca[0], ca[1], "127.0.0.1")


@pytest.mark.network  # real loopback sockets: the fake resolver would break them
class TestInstalledCaIsNeverBypassed:
    """With the daemon CA installed, a probe that cannot verify it fails."""

    def test_a_certificate_that_does_not_chain_to_the_ca_fails_the_check(
        self, tmp_path: Path
    ) -> None:
        installed, impostor = generate_ca(), generate_ca()
        with (
            _health_server(_signed_by(impostor), tmp_path) as port,
            _run_dir_at(tmp_path),
            _ca_at(tmp_path, installed[0]),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert f"fails verification against {tmp_path / 'ca.crt'}" in result.message

    def test_a_plaintext_listener_answering_ready_fails_the_check(
        self, tmp_path: Path
    ) -> None:
        installed = generate_ca()
        with (
            _health_server(None, tmp_path) as port,
            _run_dir_at(tmp_path),
            _ca_at(tmp_path, installed[0]),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert (
            f"does not complete TLS although {tmp_path / 'ca.crt'} is installed"
            in result.message
        )

    def test_a_certificate_signed_by_the_ca_passes(self, tmp_path: Path) -> None:
        installed = generate_ca()
        with (
            _health_server(_signed_by(installed), tmp_path) as port,
            _run_dir_at(tmp_path),
            _ca_at(tmp_path, installed[0]),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is True

    def test_without_a_ca_a_plaintext_daemon_still_passes(self, tmp_path: Path) -> None:
        with (
            _health_server(None, tmp_path) as port,
            _run_dir_at(tmp_path),
            patch(
                "quarry.home_paths.QuarryHome.daemon_ca",
                return_value=tmp_path / "ca.crt",
            ),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is True


@contextmanager
def _resetting_listener(*, resets: int | None = None) -> Generator[int]:
    """Listen on the loopback and reset a connection the moment it is made.

    With *resets*, only that many are reset; later ones are answered as a plain
    HTTP daemon that is ready.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.05)
    done = threading.Event()
    seen = 0

    def reset() -> None:
        nonlocal seen
        while not done.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            seen += 1
            if resets is not None and seen > resets:
                conn.recv(4096)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\n"
                    b"Connection: close\r\n\r\n"
                    b'{"state":"ready"}'
                )
                conn.close()
                continue
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            conn.close()

    thread = threading.Thread(target=reset, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        done.set()
        thread.join()
        listener.close()


@pytest.mark.network
class TestAResetHandshake:
    def test_with_a_ca_installed_it_is_a_tls_fault_not_not_ready(
        self, tmp_path: Path
    ) -> None:
        installed = generate_ca()
        with (
            _resetting_listener() as port,
            _run_dir_at(tmp_path),
            _ca_at(tmp_path, installed[0]),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert (
            f"does not complete TLS although {tmp_path / 'ca.crt'} is installed"
            in result.message
        )

    def test_without_a_ca_it_is_just_not_ready(self, tmp_path: Path) -> None:
        with (
            _resetting_listener() as port,
            _run_dir_at(tmp_path),
            patch(
                "quarry.home_paths.QuarryHome.daemon_ca",
                return_value=tmp_path / "ca.crt",
            ),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is False
        assert "not ready" in result.message

    def test_without_a_ca_a_reset_handshake_retries_over_plain_http(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _resetting_listener(resets=1) as port,
            _run_dir_at(tmp_path),
            patch(
                "quarry.home_paths.QuarryHome.daemon_ca",
                return_value=tmp_path / "ca.crt",
            ),
            caplog.at_level(logging.INFO, logger="quarry.doctor_daemon"),
        ):
            (tmp_path / "serve.port").write_text(str(port))
            result = DaemonDiagnostics.reachability()

        assert result.passed is True
        assert "retrying over plain HTTP" in caplog.text
