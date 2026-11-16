"""Doctor checks read off the daemon's ``/health`` body: fd headroom and finalize."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, final

from pydantic import ValidationError

from quarry.api.health import FdHealth, FinalizeHealth
from quarry.doctor_health_probe import HealthProbe, HealthUnavailableError
from quarry.fd_headroom import FdHeadroom
from quarry.results import CheckResult

if TYPE_CHECKING:
    from quarry.run_dir import RunDir

# With no daemon there is nothing to sample (never a local CLI fallback — that
# fallback is the observability bug itself).
_FD_UNAVAILABLE = "daemon fd headroom unavailable — daemon not running"
_FINALIZE_UNAVAILABLE = "daemon finalize state unavailable — daemon not running"


@final
class HealthBodyDiagnostics:
    """Checks whose evidence is a field of the resident daemon's ``/health``."""

    __slots__ = ()

    @classmethod
    def fd_headroom(cls, run_dir: RunDir) -> CheckResult:
        """Report the DAEMON's open-fd headroom, never the CLI's shell ``ulimit``.

        The resident daemon is the only long-lived process that accumulates
        LanceDB reader descriptors and can hit ``EMFILE``. Degrade to an advisory
        when it is unreachable or reported no headroom.
        """
        result = partial(CheckResult, name="FD headroom", required=False)
        try:
            body = HealthProbe.read(run_dir, _FD_UNAVAILABLE)
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
    def finalize(cls, run_dir: RunDir) -> CheckResult:
        """Report the daemon's finalize-optimize failure ledger.

        Only a failing optimize is required: it leaks orphaned files every
        attempt. A stopped daemon or missing state says nothing about optimize
        health and must never fail an install, so those stay advisory.
        """
        advisory = partial(CheckResult, name="Finalize", required=False)
        try:
            body = HealthProbe.read(run_dir, _FINALIZE_UNAVAILABLE)
        except HealthUnavailableError as exc:
            return advisory(passed=False, message=str(exc))
        try:
            health = FinalizeHealth.model_validate(body.get("finalize"))
        except ValidationError:
            return advisory(
                passed=False, message="daemon reachable but reported no finalize state"
            )
        failing = [d for d in health.databases if d.consecutive_failures > 0]
        if not failing:
            return advisory(passed=True, message="optimize healthy")
        detail = "; ".join(
            f"{d.database}: {d.consecutive_failures} consecutive failures, "
            f"retry in {d.retry_in_seconds:.0f}s ({d.last_error})"
            for d in failing
        )
        return CheckResult(
            name="Finalize",
            passed=False,
            message=f"optimize failing — {detail}",
            required=True,
        )

    @staticmethod
    def _fd_from_health(body: dict[str, object]) -> FdHeadroom | None:
        """Build the daemon's ``FdHeadroom`` from the ``/health`` ``fd`` field."""
        try:
            health = FdHealth.model_validate(body.get("fd"))
        except ValidationError:
            return None
        return FdHeadroom(open_fds=health.open_fds, soft_limit=health.soft_limit)
