"""The doctor check that the installed service gives the daemon time to drain."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, final

from pydantic import ValidationError

from quarry.config import Settings
from quarry.installed_budget import BudgetState
from quarry.installed_stop_timeout import InstalledStopTimeout
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult
from quarry.stop_timeout_verdict import StopTimeoutState

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@final
class StopTimeoutDiagnostic:
    """Judge an installed service's stop timeout against the daemon's budget.

    An old unit or plist carries no stop timeout, so the service manager kills
    the daemon at its own default, inside the teardown budget.  The budget it is
    judged against is the installed definition's own ``INGEST_DRAIN_TIMEOUT_S``;
    the settings' only when the definition truly sets none, and the message says
    which.  An unusable installed budget is reported with its reason, never
    replaced.  Every outcome of the read is a report, never an exception: doctor
    goes on to its other checks.
    """

    __slots__ = ()

    @classmethod
    def check(cls) -> CheckResult:
        """Return the check's result."""
        result = partial(CheckResult, name="service stop timeout", required=False)
        try:
            configured = Settings().ingest_drain_timeout_s
        except ValidationError as exc:  # a settings fault, not the service's
            fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
            return result(
                passed=False,
                message=f"the settings are invalid ({fields}); fix them to check "
                "the service stop timeout",
            )
        try:
            return cls._judge(result, configured)
        except Exception as exc:
            logger.error(
                "doctor: the stop timeout check failed",
                exc_info=RedactedFailure.of(exc),
            )
            return result(
                passed=False,
                message=f"could not check the installed service ({type(exc).__name__})",
            )

    @staticmethod
    def _judge(result: Callable[..., CheckResult], configured: float) -> CheckResult:
        """Judge the installed definition against its own budget, else *configured*."""
        installed = InstalledStopTimeout.installed()
        budget = installed.budget()
        if budget.state is BudgetState.INVALID:
            return result(passed=False, message=budget.message)
        valid = budget.state is BudgetState.VALID
        total = budget.value if valid else configured
        source = "the installed service definition" if valid else "settings"
        verdict = installed.verdict(total)
        if verdict.state is StopTimeoutState.NOT_INSTALLED:
            return result(passed=True, message=verdict.message)
        return result(
            passed=not verdict.is_problem,
            message=f"{verdict.message} (budget {total:g}s from {source})",
        )
