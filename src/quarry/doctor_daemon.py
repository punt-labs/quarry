"""Daemon-health doctor checks: quarryd reachability and the serve.token sidecar.

Fail-closed loopback auth (:class:`quarry.client.ClientConfig`) tells operators to
"Run 'quarry doctor'" when ``serve.token`` is missing, unreadable, or empty. These
checks make that remediation real: they resolve the SAME run dir the client reads
(the active database's, per :meth:`quarry.config.Settings.active_db`) and diagnose
a token/daemon outage instead of pointing at a dead end.
"""

from __future__ import annotations

import http.client
import json
import ssl
import stat
from functools import partial
from pathlib import Path
from typing import final

from quarry.config import Settings
from quarry.results import CheckResult
from quarry.run_dir import RunDir

# The literal loopback the daemon binds and login pins (never the ambiguous name
# ``localhost``); doctor probes exactly what the client connects to.
_HEALTH_HOST = "127.0.0.1"
_PROBE_TIMEOUT_SECONDS = 5.0
_TOKEN_MODE = 0o600
# The daemon's pinned CA, written by ``quarry install`` (mirrors tls.TLS_DIR);
# defined locally so this diagnostic never imports cryptography via quarry.tls.
_CA_CERT_PATH = Path.home() / ".punt-labs" / "quarry" / "tls" / "ca.crt"


@final
class DaemonDiagnostics:
    """Health checks for the quarryd daemon and its loopback-auth sidecars."""

    __slots__ = ()

    @classmethod
    def reachability(cls) -> CheckResult:
        """Report whether quarryd is up and READY on the literal loopback.

        Resolves the active-db run dir, reads ``serve.port``, and probes
        ``/health`` for ``state == "ready"`` (mirrors install.sh's gate). Fail
        soft: a missing port file or an unreachable/unready daemon is a ``✗``
        report with a start-the-service hint, never a doctor crash.
        """
        result = partial(CheckResult, name="quarryd", required=False)
        try:
            port = cls._run_dir().port_file.read()
        except (OSError, ValueError):
            return result(
                passed=False,
                message="not reachable — start the service (quarryd not running)",
            )
        if cls._probe_health(port):
            return result(
                passed=True, message=f"running and ready on {_HEALTH_HOST}:{port}"
            )
        return result(
            passed=False,
            message=f"not ready on {_HEALTH_HOST}:{port} — start or check the service",
        )

    @classmethod
    def serve_token(cls) -> CheckResult:
        """Report whether ``serve.token`` is present, mode-0600, and non-empty.

        Checks the SAME run dir the loopback client reads, so a token outage the
        client fails closed on is diagnosed here. Fail soft: a missing or
        unreadable token is a ``✗`` report (daemon down, or another UID owns the
        run dir), never a crash.
        """
        result = partial(CheckResult, name="serve.token", required=False)
        try:
            token_path = cls._run_dir().token_file.path
        except (OSError, ValueError):
            return result(
                passed=False, message="run dir unresolved — quarryd not installed"
            )
        try:
            mode = stat.S_IMODE(token_path.stat().st_mode)
            content = token_path.read_text().strip()
        except OSError:
            return result(
                passed=False,
                message=(
                    f"missing or unreadable ({token_path}) — quarryd not running "
                    "or another UID owns the run dir"
                ),
            )
        if mode != _TOKEN_MODE:
            return result(
                passed=False,
                message=f"{token_path} has mode {mode:04o}, expected 0600",
            )
        if not content:
            return result(
                passed=False,
                message=f"empty ({token_path}) — quarryd wrote a corrupt token",
            )
        return result(passed=True, message=f"present, 0600 ({token_path})")

    @staticmethod
    def _run_dir() -> RunDir:
        """The active database's run dir — the SAME one ClientConfig reads.

        Mirrors the client's resolution (the CLI's ``--db`` override, else the
        persistent default) so doctor inspects the run dir the loopback client
        actually uses, not a hardcoded default.
        """
        settings = Settings.load().resolve_db_paths(Settings.active_db() or None)
        return RunDir(settings.lancedb_path.parent)

    @classmethod
    def _probe_health(cls, port: int) -> bool:
        """Probe ``/health`` for ``state == "ready"``, over HTTPS then HTTP.

        A managed daemon serves ``--tls``; a bare ``quarryd`` (no ``--tls``)
        serves plaintext. Try HTTPS first (verify against the pinned CA when
        present, else skip verification — a loopback liveness check, not a
        security boundary; mirrors install.sh's ``--cacert`` gate and its ``-k``
        fallback). On a TLS handshake failure (``ssl.SSLError``, e.g. plaintext
        behind https / wrong-version-number), fall back to plain HTTP so a
        plaintext daemon still reports ready. Fail soft: a refused connection is
        not-ready, never a raise.
        """
        https = http.client.HTTPSConnection(
            _HEALTH_HOST,
            port,
            context=cls._ssl_context(),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        try:
            return cls._probe_conn(https)
        except ssl.SSLError:
            # Plaintext daemon behind an HTTPS probe — retry over HTTP.
            http_conn = http.client.HTTPConnection(
                _HEALTH_HOST, port, timeout=_PROBE_TIMEOUT_SECONDS
            )
            return cls._probe_conn(http_conn)

    @classmethod
    def _probe_conn(cls, conn: http.client.HTTPConnection) -> bool:
        """GET ``/health`` on *conn*; True iff a 200 body reports ``ready``.

        Fail soft on a refused/broken connection (not-ready), but let an
        ``ssl.SSLError`` propagate so :meth:`_probe_health` can retry over HTTP.
        """
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            if response.status != 200:
                return False
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ssl.SSLError):
                raise  # a TLS handshake failure: let the HTTP fallback retry
            return False
        finally:
            conn.close()
        return cls._is_ready(body)

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        """A client context pinned to the daemon CA, or verification-disabled.

        The daemon serves a self-signed cert with a ``127.0.0.1`` IP SAN, so a
        pinned-CA context verifies the literal-loopback probe. Absent the CA (a
        plaintext or not-yet-installed daemon), fall back to no verification so
        the liveness probe still connects.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if _CA_CERT_PATH.exists():
            try:
                ctx.load_verify_locations(str(_CA_CERT_PATH))
            except (OSError, ssl.SSLError):
                pass
            else:
                return ctx
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @staticmethod
    def _is_ready(body: bytes) -> bool:
        """True iff the ``/health`` JSON body reports ``state == "ready"``."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(data, dict) and data.get("state") == "ready"
