"""Probe the daemon's ``/health`` endpoint over the literal loopback."""

from __future__ import annotations

import http.client
import json
import logging
import ssl
from typing import TYPE_CHECKING, final

from quarry.daemon_ca_error import DaemonCaError
from quarry.daemon_unready_error import DaemonUnreadyError
from quarry.failure_reason import FailureReason
from quarry.home_paths import QuarryHome

if TYPE_CHECKING:
    from quarry.run_dir import RunDir

logger = logging.getLogger(__name__)

# The literal loopback the daemon binds and login pins (never the ambiguous name
# ``localhost``); doctor probes exactly what the client connects to.
HEALTH_HOST = "127.0.0.1"
_PROBE_TIMEOUT_SECONDS = 5.0


class HealthUnavailableError(Exception):
    """The daemon's ``/health`` could not be read; the message says why."""


@final
class HealthProbe:
    """Read the daemon's ``/health`` JSON, verifying TLS against the pinned CA."""

    __slots__ = ()

    @classmethod
    def read(cls, run_dir: RunDir, unavailable: str) -> dict[str, object]:
        """Return the daemon's ``/health`` body, or raise why it cannot be read.

        *unavailable* is the calling check's own "daemon not running" wording, so
        each check degrades in its own terms from one shared read.
        """
        try:
            port = run_dir.port_file.read()
        except (OSError, ValueError) as exc:
            raise HealthUnavailableError(unavailable) from exc
        try:
            body = cls.fetch(port)
        except DaemonCaError as exc:
            msg = f"{exc}; run 'quarry install'"
            raise HealthUnavailableError(msg) from exc
        except DaemonUnreadyError as exc:
            msg = f"{unavailable} ({exc})"
            raise HealthUnavailableError(msg) from exc
        if body is None:
            raise HealthUnavailableError(unavailable)
        return body

    @classmethod
    def fetch(cls, port: int) -> dict[str, object] | None:
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
            HEALTH_HOST,
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
                HEALTH_HOST, port, timeout=_PROBE_TIMEOUT_SECONDS
            )
            body = cls._read_health(http_conn)
            if body is None:
                msg = f"the TLS attempt failed first ({type(exc).__name__})"
                raise DaemonUnreadyError(msg) from exc
            return body

    @classmethod
    def _read_health(
        cls, conn: http.client.HTTPConnection, *, tls: bool = False
    ) -> dict[str, object] | None:
        """GET ``/health`` on *conn*; return the parsed JSON object or None.

        Fail soft on a refused/broken connection (``None``), but let an
        ``ssl.SSLError`` propagate so :meth:`fetch` can retry over HTTP.
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

        A reset, an abort or a broken pipe once the TCP connection was made but
        before it became an ``SSLSocket``: a refused connection never reached the
        daemon, one with no socket at all never connected, and one that dropped
        after TLS was up (a daemon restarting) is the daemon's, not the CA's.
        """
        return (
            isinstance(exc, ConnectionError)
            and not isinstance(exc, ConnectionRefusedError)
            and conn.sock is not None
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
