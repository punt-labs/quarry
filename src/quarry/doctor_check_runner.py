"""Run doctor's checks so that none of them can stop the report."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from quarry.doctor_failure import CheckFailure
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult

if TYPE_CHECKING:
    from collections.abc import Iterable

    from quarry.doctor_preconditions import Preconditions
    from quarry.doctor_runner import CheckEntry

logger = logging.getLogger(__name__)


@final
class CheckRunner:
    """Run checks (and install steps) so that whatever they raise is a result."""

    __slots__ = ()

    @staticmethod
    # None: the entry's diagnostic had nothing to report.
    def attempt(entry: CheckEntry) -> CheckResult | None:
        """Run one entry; whatever it raises becomes a failed result naming it.

        The report must always complete, so no single check, whatever it hits,
        may stop the others.  The failure keeps the entry's ``required`` flag and
        is also logged with its frames.
        """
        try:
            return entry.call()
        except Exception as exc:
            # The report line names the cause; the frames go to the log file only.
            logger.info(
                "doctor: check %s failed", entry.name, exc_info=RedactedFailure.of(exc)
            )
            return CheckResult(
                name=entry.name,
                passed=False,
                message=f"check could not run ({CheckFailure.detail(exc)})",
                required=entry.required,
            )

    @classmethod
    def run(
        cls, entries: Iterable[CheckEntry], pre: Preconditions
    ) -> list[CheckResult]:
        """Run every entry in order; return the results that have something to say.

        An entry whose needs do not hold is skipped, with a line saying what it
        lacked; a skip is never an issue, because the Home and Settings checks
        are where those faults are counted.
        """
        results = [cls._one(entry, pre) for entry in entries]
        return [result for result in results if result is not None]

    @classmethod
    # None: the entry's diagnostic had nothing to report.
    def _one(cls, entry: CheckEntry, pre: Preconditions) -> CheckResult | None:
        if (lacks := pre.missing(entry.needs)) is not None:
            return CheckResult(
                name=entry.name,
                passed=False,
                message=f"skipped (needs {lacks})",
                required=False,
            )
        return cls.attempt(entry)
