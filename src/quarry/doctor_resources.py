"""Resource-headroom doctor checks: open file descriptors vs the soft limit.

A long-lived daemon that leaks descriptors fails silently until it hits its
``RLIMIT_NOFILE`` and every ``open()`` returns ``EMFILE`` — surfacing to users as
an HTTP 500 on ``quarry find``. This check warns while headroom still exists, so
the condition is visible before it becomes an outage.
"""

from __future__ import annotations

import errno
from functools import partial
from typing import final

from quarry.fd_headroom import FdHeadroom
from quarry.results import CheckResult


@final
class ResourceDiagnostics:
    """Health checks for process resource headroom."""

    __slots__ = ()

    @staticmethod
    def fd_headroom() -> CheckResult:
        """Warn when open descriptors approach the soft ``RLIMIT_NOFILE`` limit.

        Descriptor exhaustion (``EMFILE``/``ENFILE``) raised *during* the sample
        is the very condition this check exists to catch — it reports
        ``passed=False``, never a reassuring "unavailable". Only a genuine
        platform absence of an fd directory to scan degrades to ``passed=True``.
        """
        result = partial(CheckResult, name="FD headroom", required=False)
        try:
            headroom = FdHeadroom.sample()
        except OSError as exc:
            if exc.errno in (errno.EMFILE, errno.ENFILE):
                return result(
                    passed=False,
                    message="descriptor exhaustion suspected (EMFILE)",
                )
            return result(passed=True, message=f"unavailable: {exc}")
        if headroom.is_low:
            return result(
                passed=False,
                message=f"{headroom.describe()} — over 80%, risk of EMFILE",
            )
        return result(passed=True, message=headroom.describe())
