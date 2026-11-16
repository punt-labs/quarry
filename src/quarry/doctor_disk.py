"""Doctor checks that the database is not bloated and the disk is not filling."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, ClassVar, final

from quarry.doctor_failure import CheckFailure
from quarry.results import CheckResult

if TYPE_CHECKING:
    from pathlib import Path

_GIB = 1024**3


@final
class FootprintCheck:
    """Warn when the chunks table occupies far more disk than its live data.

    A table that cannot compact leaks files on every attempt (hundreds of GB for
    a few thousand rows were seen).  A healthy table awaiting compaction holds a
    small multiple of its live size, so the ratio only counts above a floor that
    keeps a tiny table's noise from warning.
    """

    # Above the 2-5x a healthy pre-compaction table reaches, far below the
    # ~2000x of a failed one.
    MAX_RATIO: ClassVar[float] = 10.0
    MIN_DISK_BYTES: ClassVar[int] = _GIB

    __slots__ = ()

    @classmethod
    def check(cls, db_path: Path) -> CheckResult:
        """Compare the table's on-disk size with its live size; advisory only."""
        try:
            from quarry.db.table_probe import TableProbe  # noqa: PLC0415
        except CheckFailure.BROKEN_STACK as exc:
            return cls._result(
                passed=False, message=CheckFailure.import_message("quarry.db", exc)
            )
        if not db_path.exists():
            return cls._result(passed=True, message="no database yet")
        try:
            sizes = TableProbe.sizes(db_path)
        except CheckFailure.BROKEN_STACK as exc:
            return cls._result(
                passed=False, message=f"cannot measure ({CheckFailure.detail(exc)})"
            )
        if sizes.disk_bytes == 0:
            return cls._result(passed=True, message="no table yet")
        summary = (
            f"{sizes.disk_bytes / _GIB:.2f} GiB on disk, "
            f"{sizes.live_bytes / _GIB:.2f} GiB live"
        )
        if sizes.disk_bytes >= cls.MIN_DISK_BYTES and sizes.ratio > cls.MAX_RATIO:
            return cls._result(
                passed=False,
                message=(
                    f"{summary} ({sizes.ratio:.0f}x) — compaction not keeping up; "
                    "no repair command yet"
                ),
            )
        return cls._result(passed=True, message=summary)

    @staticmethod
    def _result(*, passed: bool, message: str) -> CheckResult:
        return CheckResult(
            name="Disk footprint", passed=passed, message=message, required=False
        )


@final
class FreeDiskCheck:
    """Warn when the database's filesystem is nearly full."""

    # At the ~20 GB/day growth seen on a failing table this is about half a day.
    MIN_FREE_BYTES: ClassVar[int] = 10 * _GIB

    __slots__ = ()

    @classmethod
    def check(cls, db_path: Path) -> CheckResult:
        """Report free space on the filesystem that holds (or will hold) *db_path*."""
        try:
            free = shutil.disk_usage(cls._existing(db_path)).free
        except OSError as exc:
            return cls._result(
                passed=False,
                message=f"cannot read disk usage ({CheckFailure.detail(exc)})",
            )
        message = f"{free / _GIB:.1f} GiB free"
        return cls._result(passed=free >= cls.MIN_FREE_BYTES, message=message)

    @staticmethod
    def _existing(path: Path) -> Path:
        """Return *path* or its nearest ancestor that exists."""
        return next((p for p in (path, *path.parents) if p.exists()), path)

    @staticmethod
    def _result(*, passed: bool, message: str) -> CheckResult:
        return CheckResult(
            name="Free disk", passed=passed, message=message, required=False
        )
