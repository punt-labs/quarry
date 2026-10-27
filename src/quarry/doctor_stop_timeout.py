"""The doctor check that the installed service gives the daemon time to drain."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, final

from quarry.home_paths import HomeUnresolvedError
from quarry.installed_budget import BudgetState
from quarry.installed_stop_timeout import InstalledStopTimeout
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult
from quarry.stop_timeout_verdict import StopTimeoutState

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.config import Settings

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
    def check(cls, settings: Settings) -> CheckResult:
        """Return the check's result, judged against *settings*' drain timeout."""
        result = partial(CheckResult, name="service stop timeout", required=False)
        try:
            return cls._judge(result, settings.ingest_drain_timeout_s)
        except HomeUnresolvedError:
            raise  # a precondition the runner declares, never a raw line
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
        # Nothing installed is nothing to judge: a stale env file left behind by
        # an earlier install says nothing about a service that is not there.
        if installed.verdict(configured).state is StopTimeoutState.NOT_INSTALLED:
            return result(passed=True, message="no installed service; not checked")
        budget = installed.budget()
        if budget.state is BudgetState.INVALID:
            return result(passed=False, message=budget.message)
        valid = budget.state is BudgetState.VALID
        total = budget.value if valid else configured
        source = "the installed service definition" if valid else "settings"
        verdict = installed.verdict(total)
        return result(
            passed=not verdict.is_problem,
            message=f"{verdict.message} (budget {total:g}s from {source})",
        )
