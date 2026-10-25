"""Daemon-health doctor checks: quarryd reachability, serve.token, and fd headroom.

Fail-closed loopback auth (:class:`quarry.client.ClientConfig`) tells operators to
"Run 'quarry doctor'" when ``serve.token`` is missing, unreadable, or empty. These
checks make that remediation real: they resolve the SAME run dir the client reads
(the active database's, per :meth:`quarry.db_pointer.DatabaseSelection.active`)
and diagnose a token/daemon outage instead of pointing at a dead end.

Descriptor headroom is likewise a property of the resident daemon — the only
long-lived process that accumulates LanceDB reader handles and can hit EMFILE —
so :meth:`DaemonDiagnostics.fd_headroom` reads the daemon's self-sampled ``fd``
field off ``/health`` rather than sampling the short-lived CLI, whose limit is
just the invoking shell's ``ulimit``.
"""

from __future__ import annotations

import http.client
import json
import ssl
import stat
from functools import partial
from pathlib import Path
from typing import final

from pydantic import ValidationError

from quarry.api.meta import FdHealth
from quarry.config import Settings
from quarry.db_pointer import SELECTION
from quarry.doctor_stop_timeout import StopTimeoutDiagnostic
from quarry.fd_headroom import FdHeadroom
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
# Reported when the daemon's fd headroom cannot be read: fd headroom is a
# property of the resident daemon, so with no daemon there is nothing to sample
# (never a local CLI fallback — that fallback is the observability bug itself).
_FD_UNAVAILABLE = "daemon fd headroom unavailable — daemon not running"


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

    @staticmethod
    def stop_timeout() -> CheckResult:
        """Report whether the installed service gives the daemon time to drain."""
        return StopTimeoutDiagnostic.check()

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

    @classmethod
    def fd_headroom(cls) -> CheckResult:
        """Report the DAEMON's open-fd headroom, read from its ``/health``.

        The resident daemon is the only long-lived process that accumulates
        LanceDB reader descriptors and can hit ``EMFILE``, so its headroom — not
        the short-lived CLI's shell ``ulimit`` — is what this check must surface.
        Sampling locally would report the invoking shell's limit, meaningless for
        the daemon; reading the daemon's self-sampled ``fd`` field is the fix.
        Degrade to a clear advisory when the daemon is unreachable or reported no
        fd headroom; never fall back to a local sample — that fallback IS the bug.
        """
        result = partial(CheckResult, name="FD headroom", required=False)
        try:
            port = cls._run_dir().port_file.read()
        except (OSError, ValueError):
            return result(passed=False, message=_FD_UNAVAILABLE)
        body = cls._fetch_health(port)
        if body is None:
            return result(passed=False, message=_FD_UNAVAILABLE)
        headroom = cls._fd_from_health(body)
        if headroom is None:
            return result(
                passed=False,
                message="daemon reachable but reported no fd headroom",
            )
        if headroom.is_low:
            return result(
                passed=False,
                message=f"daemon {headroom.describe()} — over 80%, "
                "risk of descriptor exhaustion",
            )
        return result(passed=True, message=f"daemon {headroom.describe()}")

    @staticmethod
    def _fd_from_health(body: dict[str, object]) -> FdHeadroom | None:
        """Build the daemon's ``FdHeadroom`` from the ``/health`` ``fd`` field.

        Validates the wire value through :class:`FdHealth` — the single owner of
        the fd shape — returning ``None`` (the "no headroom to render" signal)
        when the daemon reported ``fd: null`` or the field is malformed.
        """
        try:
            health = FdHealth.model_validate(body.get("fd"))
        except ValidationError:
            return None
        return FdHeadroom(open_fds=health.open_fds, soft_limit=health.soft_limit)

    @staticmethod
    def _run_dir() -> RunDir:
        """The active database's run dir — the SAME one ClientConfig reads.

        Mirrors the client's resolution (the CLI's ``--db`` override, else the
        persistent default) so doctor inspects the run dir the loopback client
        actually uses, not a hardcoded default.
        """
        settings = Settings.load().resolve_db_paths(SELECTION.active())
        return RunDir(settings.lancedb_path.parent)

    @classmethod
    def _probe_health(cls, port: int) -> bool:
        """Return True iff ``/health`` reports ``state == "ready"``."""
        body = cls._fetch_health(port)
        return body is not None and body.get("state") == "ready"

    @classmethod
    def _fetch_health(cls, port: int) -> dict[str, object] | None:
        """GET ``/health`` over HTTPS then HTTP; return the parsed object or None.

        The single daemon-health read that both :meth:`reachability` and
        :meth:`fd_headroom` consume, so the two checks never drift into separate
        probes. A managed daemon serves ``--tls``; a bare
        ``quarryd`` (no ``--tls``) serves plaintext. Try HTTPS first (verify
        against the pinned CA when present, else skip verification — a loopback
        liveness check, not a security boundary; mirrors install.sh's ``--cacert``
        gate and its ``-k`` fallback). On a TLS handshake failure (plaintext
        behind https / wrong-version-number), fall back to plain HTTP so a
        plaintext daemon still answers. Returns ``None`` — the documented
        not-reachable signal — on a refused/broken connection, a non-200, or a
        non-object body, so callers render one degraded state.
        """
        https = http.client.HTTPSConnection(
            _HEALTH_HOST,
            port,
            context=cls._ssl_context(),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        try:
            return cls._read_health(https)
        except ssl.SSLError:
            # Plaintext daemon behind an HTTPS probe — retry over HTTP.
            http_conn = http.client.HTTPConnection(
                _HEALTH_HOST, port, timeout=_PROBE_TIMEOUT_SECONDS
            )
            return cls._read_health(http_conn)

    @classmethod
    def _read_health(cls, conn: http.client.HTTPConnection) -> dict[str, object] | None:
        """GET ``/health`` on *conn*; return the parsed JSON object or None.

        Fail soft on a refused/broken connection (``None``), but let an
        ``ssl.SSLError`` propagate so :meth:`_fetch_health` can retry over HTTP.
        """
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            if response.status != 200:
                return None
            body = response.read()
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ssl.SSLError):
                raise  # a TLS handshake failure: let the HTTP fallback retry
            return None
        finally:
            conn.close()
        return cls._parse_object(body)

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
    def _parse_object(body: bytes) -> dict[str, object] | None:
        """Return the ``/health`` JSON body as a dict, or None if not an object."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None
