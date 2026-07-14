"""Open file descriptors measured against the process's ``RLIMIT_NOFILE``.

A long-lived daemon that leaks descriptors fails silently until it hits its soft
limit and every ``open()`` returns ``EMFILE`` — surfacing to users as an HTTP 500
on ``quarry find``. This value object samples the current count so both the doctor
health check and the serve-time telemetry can warn before the wall is hit.

The measurement itself needs a file descriptor (the ``iterdir`` scan opens a
directory handle). That means, at real exhaustion, the sample *is what fails* —
so ``sample`` deliberately lets an ``EMFILE``/``ENFILE`` ``OSError`` propagate
rather than masking it. Callers distinguish that exhaustion signal from a
platform that simply lacks an fd directory to scan by inspecting ``errno``.
"""

from __future__ import annotations

import resource
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

# Warn once the process crosses this fraction of its soft descriptor limit —
# early enough to act before EMFILE, late enough to avoid nuisance warnings.
_FD_WARN_RATIO = 0.8


@final
@dataclass(frozen=True, slots=True)
class FdHeadroom:
    """Open file descriptors measured against the soft ``RLIMIT_NOFILE`` ceiling."""

    open_fds: int
    soft_limit: int

    @classmethod
    def sample(cls) -> Self:
        """Measure the current process's open descriptors and soft fd limit.

        Propagates ``OSError`` whose ``errno`` is ``EMFILE``/``ENFILE`` when
        descriptor exhaustion prevents the scan itself — that failure *is* the
        signal callers must surface, never swallow as healthy.
        """
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return cls(open_fds=cls._count_open_fds(), soft_limit=soft)

    @staticmethod
    def _count_open_fds() -> int:
        """Return this process's open-descriptor count via ``/proc`` or ``/dev/fd``.

        Raises a plain ``OSError`` (``errno`` unset) only when neither directory
        exists — genuine platform absence. Under real descriptor exhaustion the
        ``iterdir`` scan raises ``OSError(EMFILE)`` and that propagates unchanged,
        so the caller can tell exhaustion from absence by ``errno``.
        """
        for fd_dir in ("/proc/self/fd", "/dev/fd"):
            path = Path(fd_dir)
            if path.is_dir():
                return sum(1 for _ in path.iterdir())
        msg = "no /proc/self/fd or /dev/fd on this platform"
        raise OSError(msg)

    @property
    def _is_bounded(self) -> bool:
        """Whether the soft limit is a real positive ceiling, not ``RLIM_INFINITY``."""
        # RLIM_INFINITY is a large positive int, not 0, yet still means unbounded.
        return self.soft_limit > 0 and self.soft_limit != resource.RLIM_INFINITY

    @property
    def utilization(self) -> float:
        """Fraction of the soft limit in use (``0.0`` when the limit is unbounded)."""
        if not self._is_bounded:
            return 0.0
        return self.open_fds / self.soft_limit

    @property
    def is_low(self) -> bool:
        """Whether descriptor usage has crossed the warning threshold."""
        return self.utilization > _FD_WARN_RATIO

    def describe(self) -> str:
        """Return a compact ``used/limit (pct%)`` summary.

        Renders ``used/unlimited fds`` when the soft limit is unbounded, rather
        than printing the giant ``RLIM_INFINITY`` sentinel and a meaningless 0%.
        """
        if not self._is_bounded:
            return f"{self.open_fds}/unlimited fds"
        return (
            f"{self.open_fds}/{self.soft_limit} fds ({round(self.utilization * 100)}%)"
        )
