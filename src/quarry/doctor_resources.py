"""Resource-headroom doctor checks: open file descriptors vs the soft limit.

A long-lived daemon that leaks descriptors fails silently until it hits its
``RLIMIT_NOFILE`` and every ``open()`` returns ``EMFILE`` — surfacing to users as
an HTTP 500 on ``quarry find``. This check warns while headroom still exists, so
the condition is visible before it becomes an outage.
"""

from __future__ import annotations

import resource
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Self, final

from quarry.results import CheckResult

# Warn once the process crosses this fraction of its soft descriptor limit —
# early enough to act before EMFILE, late enough to avoid nuisance warnings.
_FD_WARN_RATIO = 0.8


@dataclass(frozen=True, slots=True)
class FdHeadroom:
    """Open file descriptors measured against the soft ``RLIMIT_NOFILE`` ceiling."""

    open_fds: int
    soft_limit: int

    @classmethod
    def sample(cls) -> Self:
        """Measure the current process's open descriptors and soft fd limit."""
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return cls(open_fds=cls._count_open_fds(), soft_limit=soft)

    @staticmethod
    def _count_open_fds() -> int:
        """Return this process's open-descriptor count via ``/proc`` or ``/dev/fd``."""
        for fd_dir in ("/proc/self/fd", "/dev/fd"):
            path = Path(fd_dir)
            if path.is_dir():
                return sum(1 for _ in path.iterdir())
        msg = "no /proc/self/fd or /dev/fd on this platform"
        raise OSError(msg)

    @property
    def utilization(self) -> float:
        """Fraction of the soft limit in use (``0.0`` when the limit is unbounded)."""
        if self.soft_limit <= 0:
            return 0.0
        return self.open_fds / self.soft_limit

    @property
    def is_low(self) -> bool:
        """Whether descriptor usage has crossed the warning threshold."""
        return self.utilization > _FD_WARN_RATIO

    def describe(self) -> str:
        """Return a compact ``used/limit (pct%)`` summary."""
        return (
            f"{self.open_fds}/{self.soft_limit} fds ({round(self.utilization * 100)}%)"
        )


@final
class ResourceDiagnostics:
    """Health checks for process resource headroom."""

    __slots__ = ()

    @staticmethod
    def fd_headroom() -> CheckResult:
        """Warn when open descriptors approach the soft ``RLIMIT_NOFILE`` limit."""
        result = partial(CheckResult, name="FD headroom", required=False)
        try:
            headroom = FdHeadroom.sample()
        except (OSError, ValueError) as exc:
            return result(passed=True, message=f"unavailable: {exc}")
        if headroom.is_low:
            return result(
                passed=False,
                message=f"{headroom.describe()} — over 80%, risk of EMFILE",
            )
        return result(passed=True, message=headroom.describe())
