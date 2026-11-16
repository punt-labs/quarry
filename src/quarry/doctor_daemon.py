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

import stat
from functools import partial
from typing import TYPE_CHECKING, final

from quarry.daemon_ca_error import DaemonCaError
from quarry.daemon_unready_error import DaemonUnreadyError
from quarry.db_pointer import SELECTION
from quarry.doctor_health import HealthBodyDiagnostics
from quarry.doctor_health_probe import HEALTH_HOST, HealthProbe
from quarry.doctor_stop_timeout import StopTimeoutDiagnostic
from quarry.results import CheckResult
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from quarry.config import Settings

_TOKEN_MODE = 0o600


@final
class DaemonDiagnostics:
    """Health checks for the quarryd daemon and its loopback-auth sidecars."""

    __slots__ = ()

    @classmethod
    def reachability(cls, settings: Settings) -> CheckResult:
        """Report whether quarryd is up and READY on the literal loopback.

        Resolves the active-db run dir from *settings*, reads ``serve.port``, and probes
        ``/health`` for ``state == "ready"`` (mirrors install.sh's gate). Fail
        soft: a missing port file or an unreachable/unready daemon is a ``✗``
        report with a start-the-service hint, never a doctor crash.
        """
        result = partial(CheckResult, name="quarryd", required=False)
        try:
            port = cls._run_dir(settings).port_file.read()
        except (OSError, ValueError):
            return result(
                passed=False,
                message="not reachable — start the service (quarryd not running)",
            )
        try:
            is_ready = cls._probe_health(port)
        except DaemonCaError as exc:
            return result(passed=False, message=f"{exc}; run 'quarry install'")
        except DaemonUnreadyError as exc:
            return result(
                passed=False,
                message=f"not ready on {HEALTH_HOST}:{port} — {exc}; "
                "start or check the service",
            )
        if is_ready:
            return result(
                passed=True, message=f"running and ready on {HEALTH_HOST}:{port}"
            )
        return result(
            passed=False,
            message=f"not ready on {HEALTH_HOST}:{port} — start or check the service",
        )

    @staticmethod
    def stop_timeout(settings: Settings) -> CheckResult:
        """Report whether the installed service gives the daemon time to drain."""
        return StopTimeoutDiagnostic.check(settings)

    @classmethod
    def serve_token(cls, settings: Settings) -> CheckResult:
        """Report whether ``serve.token`` is present, mode-0600, and non-empty.

        Checks the SAME run dir the loopback client reads, so a token outage the
        client fails closed on is diagnosed here. Fail soft: a missing or
        unreadable token is a ``✗`` report (daemon down, or another UID owns the
        run dir), never a crash.
        """
        result = partial(CheckResult, name="serve.token", required=False)
        try:
            token_path = cls._run_dir(settings).token_file.path
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
    def fd_headroom(cls, settings: Settings) -> CheckResult:
        """Report the DAEMON's open-fd headroom, read from its ``/health``."""
        return HealthBodyDiagnostics.fd_headroom(cls._run_dir(settings))

    @classmethod
    def finalize(cls, settings: Settings) -> CheckResult:
        """Report the daemon's finalize-optimize failure ledger from ``/health``."""
        return HealthBodyDiagnostics.finalize(cls._run_dir(settings))

    @staticmethod
    def _run_dir(settings: Settings) -> RunDir:
        """The active database's run dir — the SAME one ClientConfig reads.

        Mirrors the client's resolution (the CLI's ``--db`` override, else the
        persistent default) so doctor inspects the run dir the loopback client
        actually uses, not a hardcoded default.
        """
        paths = settings.resolve_db_paths(SELECTION.active())
        return RunDir(paths.lancedb_path.parent)

    @classmethod
    def _probe_health(cls, port: int) -> bool:
        """Return True iff ``/health`` reports ``state == "ready"``."""
        body = HealthProbe.fetch(port)
        return body is not None and body.get("state") == "ready"
