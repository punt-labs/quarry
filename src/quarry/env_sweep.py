"""The temp files a crashed env-file write left behind: find them, remove the stale."""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from collections import defaultdict
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class Leftover(Enum):
    """What became of a leftover the sweep looked at."""

    REMOVED = auto()
    NON_REGULAR = auto()  # a symlink or directory: a sign of tampering
    FOREIGN = auto()  # owned by another user
    FAILED = auto()  # the filesystem refused; the entry carries the errno


@final
class EnvSweep:
    """Remove the stale temps beside an env file, and say what it left and why.

    Only names ``EnvFile`` itself creates are looked at: ``<name>.<12 hex>.tmp``
    and the legacy ``<name>.tmp``.  Of those, a symlink or a directory is never
    touched (and is reported on its own, as it should not be there), neither is a
    file another user owns, and a regular file this user owns is removed once it
    is :attr:`STALE_AFTER_S` old or dated more than :attr:`CLOCK_SKEW_S` ahead.
    A leftover that vanishes mid-sweep (a live writer renamed it) counts as
    nothing.  The names are safe to log; an errno never quotes a path.
    """

    # Long past any live write: a temp this old belongs to a crashed one.
    STALE_AFTER_S: ClassVar[float] = 600.0
    # How far ahead of the clock an mtime may be before it is not a live writer's:
    # a clock that was stepped back, or a shared filesystem a little ahead of us.
    CLOCK_SKEW_S: ClassVar[float] = 3600.0

    __slots__ = ("_directory", "_name")

    _directory: Path
    _name: re.Pattern[str]

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._directory = path.parent
        self._name = re.compile(re.escape(path.name) + r"(?:\.[0-9a-f]{12})?\.tmp")
        return self

    def run(self) -> None:
        """Sweep the directory once; never raise."""
        names = self._leftover_names()
        outcome: defaultdict[Leftover, list[str]] = defaultdict(list)
        for name in names:
            result = self._visit(name)
            if result is not None:
                outcome[result[0]].append(result[1])
        self._report(outcome)

    def _leftover_names(self) -> list[str]:
        """Return the sorted names that look like a crashed write's temp."""
        try:
            with os.scandir(self._directory) as entries:
                found = [e.name for e in entries if self._name.fullmatch(e.name)]
        except OSError as exc:
            logger.warning(
                "env file: could not list %s for leftover temp files (%s)",
                self._directory,
                FailureReason.os_name(exc),
            )
            return []
        return sorted(found)

    def _visit(self, name: str) -> tuple[Leftover, str] | None:
        """Return how *name* was dealt with, or ``None`` if it needs no word."""
        leftover = self._directory / name
        try:
            info = leftover.lstat()
            if not stat.S_ISREG(info.st_mode):
                return Leftover.NON_REGULAR, name
            if info.st_uid != os.getuid():
                return Leftover.FOREIGN, name
            if self._is_young(info.st_mtime):
                return None
            leftover.unlink()
        except FileNotFoundError:
            return None  # lost a race with whoever renamed or removed it
        except OSError as exc:
            return Leftover.FAILED, f"{name} ({FailureReason.os_name(exc)})"
        return Leftover.REMOVED, name

    @classmethod
    def _is_young(cls, mtime: float) -> bool:
        """Return whether *mtime* is recent enough to belong to a live writer."""
        return -cls.CLOCK_SKEW_S <= time.time() - mtime < cls.STALE_AFTER_S

    @staticmethod
    def _report(outcome: dict[Leftover, list[str]]) -> None:
        """Warn once per kind of thing found, with the names and the counts."""
        if not outcome:
            return
        if non_regular := outcome.get(Leftover.NON_REGULAR):
            logger.warning(
                "env file: %d non-regular leftover(s) named like a temp file were "
                "not touched (a symlink or directory is not ours to remove): %s",
                len(non_regular),
                ", ".join(non_regular),
            )
        rest = {
            kind: names
            for kind, names in outcome.items()
            if kind is not Leftover.NON_REGULAR
        }
        if not rest:
            return
        logger.warning(
            "env file: swept %d stale temp file(s), left %d foreign-owned and "
            "%d failed: %s",
            len(rest.get(Leftover.REMOVED, ())),
            len(rest.get(Leftover.FOREIGN, ())),
            len(rest.get(Leftover.FAILED, ())),
            ", ".join(name for names in rest.values() for name in names),
        )
