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

from pydantic import ValidationError

from quarry.api.health import FdHealth, FinalizeHealth
from quarry.daemon_ca_error import DaemonCaError
from quarry.daemon_unready_error import DaemonUnreadyError
from quarry.db_pointer import SELECTION
from quarry.doctor_health_probe import HEALTH_HOST, HealthProbe, HealthUnavailableError
from quarry.doctor_stop_timeout import StopTimeoutDiagnostic
from quarry.fd_headroom import FdHeadroom
from quarry.results import CheckResult
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from quarry.config import Settings

_TOKEN_MODE = 0o600
# Reported when the daemon's fd headroom cannot be read: fd headroom is a
# property of the resident daemon, so with no daemon there is nothing to sample
# (never a local CLI fallback — that fallback is the observability bug itself).
_FD_UNAVAILABLE = "daemon fd headroom unavailable — daemon not running"
_FINALIZE_UNAVAILABLE = "daemon finalize state unavailable — daemon not running"


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
            body = HealthProbe.read(cls._run_dir(settings), _FD_UNAVAILABLE)
        except HealthUnavailableError as exc:
            return result(passed=False, message=str(exc))
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

    @classmethod
    def finalize(cls, settings: Settings) -> CheckResult:
        """Report the daemon's finalize-optimize failure ledger from ``/health``.

        The ledger lives in the resident daemon, so it is read off the same
        ``/health`` body as fd headroom; a failing optimize leaks orphaned files
        every attempt and is otherwise visible only in the daemon log.
        """
        result = partial(CheckResult, name="Finalize", required=False)
        try:
            body = HealthProbe.read(cls._run_dir(settings), _FINALIZE_UNAVAILABLE)
        except HealthUnavailableError as exc:
            return result(passed=False, message=str(exc))
        try:
            health = FinalizeHealth.model_validate(body.get("finalize"))
        except ValidationError:
            return result(
                passed=False, message="daemon reachable but reported no finalize state"
            )
        failing = [d for d in health.databases if d.consecutive_failures > 0]
        if not failing:
            return result(passed=True, message="optimize healthy")
        detail = "; ".join(
            f"{d.database}: {d.consecutive_failures} consecutive failures, "
            f"retry in {d.retry_in_seconds:.0f}s ({d.last_error})"
            for d in failing
        )
        return result(passed=False, message=f"optimize failing — {detail}")

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
