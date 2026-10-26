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
import logging
import ssl
import stat
from functools import partial
from typing import final

from pydantic import ValidationError

from quarry.api.meta import FdHealth
from quarry.config import Settings
from quarry.daemon_ca_error import DaemonCaError
from quarry.db_pointer import SELECTION
from quarry.doctor_stop_timeout import StopTimeoutDiagnostic
from quarry.failure_reason import FailureReason
from quarry.fd_headroom import FdHeadroom
from quarry.home_paths import QuarryHome
from quarry.results import CheckResult
from quarry.run_dir import RunDir

logger = logging.getLogger(__name__)

# The literal loopback the daemon binds and login pins (never the ambiguous name
# ``localhost``); doctor probes exactly what the client connects to.
_HEALTH_HOST = "127.0.0.1"
_PROBE_TIMEOUT_SECONDS = 5.0
_TOKEN_MODE = 0o600
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
        try:
            is_ready = cls._probe_health(port)
        except DaemonCaError as exc:
            return result(passed=False, message=f"{exc}; run 'quarry install'")
        if is_ready:
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
        try:
            body = cls._fetch_health(port)
        except DaemonCaError as exc:
            return result(passed=False, message=f"{exc}; run 'quarry install'")
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
        """GET ``/health`` over HTTPS, then HTTP only if no CA is installed.

        The single daemon-health read that both :meth:`reachability` and
        :meth:`fd_headroom` consume, so the two checks never drift into separate
        probes.  With the daemon CA installed the daemon is a managed ``--tls``
        one, and the probe verifies against that CA: a certificate that does not
        chain to it, or a listener that does not speak TLS at all, is a fault
        (:class:`DaemonCaError`), never a reason to ask again in plaintext, which
        would pass a spoofed daemon.  With no CA (a bare ``quarryd`` serves
        plaintext, or nothing is installed yet) verification is off, as it is in
        install.sh's ``-k`` fallback, and a TLS handshake failure retries over
        plain HTTP.  Returns ``None`` — the documented not-reachable signal — on
        a refused/broken connection, a non-200, or a non-object body, so callers
        render one degraded state.
        """
        ca = QuarryHome.daemon_ca()  # before anything is built: no home raises here
        pinned = cls._pinned_context()
        https = http.client.HTTPSConnection(
            _HEALTH_HOST,
            port,
            context=pinned or cls._unverified_context(),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        try:
            return cls._read_health(https, tls=True)
        except ssl.SSLCertVerificationError as exc:
            reason = exc.verify_message or type(exc).__name__
            msg = f"the daemon's certificate fails verification against {ca} ({reason})"
            raise DaemonCaError(msg) from exc
        except (ssl.SSLError, ConnectionError) as exc:
            if pinned is not None:
                tls_reason = exc.reason if isinstance(exc, ssl.SSLError) else None
                reason = tls_reason or type(exc).__name__
                msg = (
                    f"the daemon does not complete TLS although {ca} is installed "
                    f"({reason})"
                )
                raise DaemonCaError(msg) from exc
            logger.info(
                "doctor: the TLS handshake failed with no CA pinned (%s); retrying "
                "over plain HTTP",
                type(exc).__name__,
            )
            http_conn = http.client.HTTPConnection(
                _HEALTH_HOST, port, timeout=_PROBE_TIMEOUT_SECONDS
            )
            return cls._read_health(http_conn)

    @classmethod
    def _read_health(
        cls, conn: http.client.HTTPConnection, *, tls: bool = False
    ) -> dict[str, object] | None:
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
            if isinstance(exc, ssl.SSLError) or (
                tls and cls._dropped_before_tls(conn, exc)
            ):
                raise  # a TLS handshake failure: the caller decides what it means
            return None
        finally:
            conn.close()
        return cls._parse_object(body)

    @staticmethod
    def _dropped_before_tls(conn: http.client.HTTPConnection, exc: Exception) -> bool:
        """Return whether *exc* dropped an HTTPS connection before TLS was up.

        A reset, an abort or a broken pipe, which may arrive as the connection is
        made (then there is no socket yet) or in the handshake; a refused
        connection never reached the daemon, so it is not one of these.
        """
        return (
            isinstance(exc, ConnectionError)
            and not isinstance(exc, ConnectionRefusedError)
            and not isinstance(conn.sock, ssl.SSLSocket)
        )

    @staticmethod
    def _pinned_context() -> ssl.SSLContext | None:  # None: no CA is installed
        """Return a client context pinned to the daemon CA, or ``None`` if absent.

        The daemon serves a self-signed cert with a ``127.0.0.1`` IP SAN, so a
        pinned-CA context verifies the literal-loopback probe.  A CA that is
        there but cannot be used is a fault to report: falling back to no
        verification would pass a spoofed daemon.
        """
        ca = QuarryHome.daemon_ca()
        try:
            if not ca.exists():
                return None
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(str(ca))
        except ssl.SSLError as exc:
            msg = f"{ca} is not a usable certificate ({exc.reason})"
            raise DaemonCaError(msg) from exc
        except OSError as exc:
            msg = f"{ca} cannot be read ({FailureReason.os_name(exc)})"
            raise DaemonCaError(msg) from exc
        return ctx

    @staticmethod
    def _unverified_context() -> ssl.SSLContext:
        """Return a client context that skips verification: a liveness probe only."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
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
