"""The stop timeout an installed service definition carries, judged by policy."""

from __future__ import annotations

import logging
import platform
import plistlib
import re
from pathlib import Path
from typing import ClassVar, Self, final

from quarry.shutdown_policy import ShutdownPolicy

logger = logging.getLogger(__name__)


@final
class InstalledStopTimeout:
    """Read ``ExitTimeOut`` (plist) or ``TimeoutStopSec`` (unit) off the installed file.

    A service installed before the stop timeout existed carries none, and the
    service manager then kills the daemon at its own default, well inside the
    teardown budget.  The daemon at start and ``quarry doctor`` both ask
    :meth:`problem`, so they never disagree.  A file that is absent or unreadable
    gives no verdict: there is nothing installed to compare, or nothing to read.
    """

    _UNIT_LINE: ClassVar[re.Pattern[str]] = re.compile(
        r"^TimeoutStopSec=(\d+)\s*$", re.MULTILINE
    )

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @classmethod
    def installed(cls) -> Self:
        """Return the reader of this platform's installed service definition.

        The paths are the ones ``quarry.service`` writes (a test pins that they
        agree); they are held here so a diagnostic never imports the service
        module, and its TLS code, to find a file.
        """
        home = Path.home()
        if platform.system() == "Darwin":
            return cls(home / "Library" / "LaunchAgents" / "com.punt-labs.quarry.plist")
        return cls(home / ".config" / "systemd" / "user" / "quarry.service")

    @property
    def path(self) -> Path:
        """Return the service definition this reads."""
        return self._path

    def problem(self, total_s: float) -> str:
        """Return what is wrong with the installed stop timeout, or ``""``.

        *total_s* is the teardown budget the daemon will spend.
        """
        try:
            raw = self._path.read_bytes()
        except OSError:
            return ""  # not installed here, or not readable: nothing to judge
        found = self._read(raw)
        needed = ShutdownPolicy.stop_timeout_for(total_s)
        if found is not None and found >= needed:
            return ""
        have = "has none" if found is None else f"is {found}s"
        return (
            f"the installed service stop timeout {have}, below the {needed}s the "
            "daemon's shutdown needs, so the service manager may kill it mid-drain; "
            "run 'quarry install' to update it"
        )

    def log_if_short(self, total_s: float) -> None:
        """Log :meth:`problem` at ERROR, if there is one."""
        if problem := self.problem(total_s):
            logger.error("%s", problem)

    def _read(self, raw: bytes) -> int | None:
        """Return the stop timeout in *raw*, or ``None`` if it carries none."""
        if self._path.suffix == ".plist":
            try:
                value = plistlib.loads(raw).get("ExitTimeOut")
            except (plistlib.InvalidFileException, ValueError):
                return None
            return value if isinstance(value, int) else None
        match = self._UNIT_LINE.search(raw.decode("utf-8", errors="replace"))
        return int(match.group(1)) if match else None
