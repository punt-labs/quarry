"""The stop timeout an installed service definition carries, judged by policy."""

from __future__ import annotations

import logging
import math
import platform
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self, final
from xml.parsers.expat import ExpatError

from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy
from quarry.stop_timeout_verdict import StopTimeoutState, StopTimeoutVerdict

logger = logging.getLogger(__name__)

# What plistlib raises for a file that is not a plist: truncated or malformed XML,
# an unknown format, a number past 64 bits.
_PLIST_ERRORS = (
    ExpatError,
    plistlib.InvalidFileException,
    ValueError,
    OverflowError,
)


@final
@dataclass(frozen=True, slots=True)
class _Reading:
    """What reading a service file's stop timeout found, before the policy judges it."""

    value: float | None = None  # seconds; None: the file sets none
    problem: StopTimeoutState | None = None
    detail: str = ""


@final
class InstalledStopTimeout:
    """Read ``ExitTimeOut`` (plist) or ``TimeoutStopSec`` (unit) off the installed file.

    A service installed before the stop timeout existed carries none, and the
    service manager then kills the daemon at its own default, well inside the
    teardown budget.  The daemon at start and ``quarry doctor`` both ask
    :meth:`verdict`, so they never disagree, and every outcome of the read has
    its own verdict: a missing file, an unreadable one, one that does not parse,
    a value that cannot be interpreted, ``infinity``, a short value, a good one.

    The budget the verdict is judged against is the daemon's own at start.
    Doctor, which runs in a different process, takes it from the installed
    definition when that carries one (:meth:`budget`) and from settings otherwise,
    and says which.
    """

    _UNIT_LINE: ClassVar[re.Pattern[str]] = re.compile(
        r"^[ \t]*TimeoutStopSec[ \t]*=[ \t]*(.*?)[ \t]*$", re.MULTILINE
    )
    _NUMBER: ClassVar[str] = r"(?<!\d)\d{1,9}(?:\.\d{1,9})?(?!\d)"
    _SPAN: ClassVar[re.Pattern[str]] = re.compile(rf"(?:\s*{_NUMBER}\s*[a-z]*)+")
    _TERM: ClassVar[re.Pattern[str]] = re.compile(rf"({_NUMBER})\s*([a-z]*)")
    # systemd time-span units, in seconds; a bare number is seconds.
    _UNITS: ClassVar[dict[str, float]] = {
        "": 1.0,
        "us": 1e-6,
        "usec": 1e-6,
        "ms": 1e-3,
        "msec": 1e-3,
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "m": 60.0,
        "min": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "d": 86400.0,
        "day": 86400.0,
        "days": 86400.0,
    }

    __slots__ = ("_env_file", "_path")

    _env_file: Path | None  # None: the plist carries the environment itself
    _path: Path

    def __new__(cls, path: Path, env_file: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._env_file = env_file
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
        return cls(
            home / ".config" / "systemd" / "user" / "quarry.service",
            home / ".punt-labs" / "quarry" / "quarry.env",
        )

    @property
    def path(self) -> Path:
        """Return the service definition this reads."""
        return self._path

    def verdict(self, total_s: float) -> StopTimeoutVerdict:
        """Return what the installed stop timeout comes to for a *total_s* budget."""
        needed = ShutdownPolicy.stop_timeout_for(total_s)
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return self._verdict(StopTimeoutState.NOT_INSTALLED, needed)
        except OSError as exc:
            detail = FailureReason.os_name(exc)
            return self._verdict(StopTimeoutState.UNREADABLE, needed, detail=detail)
        is_plist = self._path.suffix == ".plist"
        reading = self._plist(raw) if is_plist else self._unit(raw)
        found = reading.value
        if reading.problem is not None:
            return self._verdict(reading.problem, needed, detail=reading.detail)
        if found is not None and math.isinf(found):
            return self._verdict(StopTimeoutState.INFINITE, needed)
        if found is None or found < needed:
            return self._verdict(StopTimeoutState.SHORT, needed, value=found)
        return self._verdict(StopTimeoutState.OK, needed, value=found)

    def budget(self, settings_total: float) -> tuple[float, str]:
        """Return the teardown budget to judge by, and where it came from.

        The installed definition's ``INGEST_DRAIN_TIMEOUT_S`` (the number the
        stop timeout was computed from) when it carries one that parses; else
        *settings_total*.
        """
        installed = self._installed_budget()
        if installed is None:
            return settings_total, "settings"
        return installed, "the installed service definition"

    def report(self, total_s: float) -> None:
        """Log the verdict: ERROR when short, WARNING for another problem; never raise.

        A diagnostic must never stop the daemon from starting, so an unexpected
        failure of the check itself is logged, by class and frames, and swallowed.
        """
        try:
            verdict = self.verdict(total_s)
        except Exception as exc:
            logger.error(
                "the installed service stop timeout could not be checked",
                exc_info=RedactedFailure.of(exc),
            )
            return
        if verdict.state is StopTimeoutState.SHORT:
            logger.error("%s", verdict.message)
        elif verdict.is_problem:
            logger.warning("%s", verdict.message)

    def _verdict(
        self,
        state: StopTimeoutState,
        needed: int,
        *,
        detail: str = "",
        value: float | None = None,
    ) -> StopTimeoutVerdict:
        """Return a verdict about this file."""
        return StopTimeoutVerdict(state, self._path, detail, value, needed)

    @staticmethod
    def _plist(raw: bytes) -> _Reading:
        """Return the plist's ``ExitTimeOut``, or why it cannot be read."""
        try:
            root = plistlib.loads(raw)
        except _PLIST_ERRORS as exc:
            return _Reading(
                problem=StopTimeoutState.UNPARSEABLE, detail=type(exc).__name__
            )
        if not isinstance(root, dict):
            return _Reading(
                problem=StopTimeoutState.UNPARSEABLE, detail="root is not a dictionary"
            )
        value = root.get("ExitTimeOut")
        if value is None:
            return _Reading()
        if isinstance(value, bool) or not isinstance(value, int):
            return _Reading(
                problem=StopTimeoutState.UNRECOGNIZED, detail=repr(value)[:40]
            )
        return _Reading(float(value))

    @classmethod
    def _unit(cls, raw: bytes) -> _Reading:
        """Return the unit's last ``TimeoutStopSec``, or why it cannot be read."""
        text = raw.decode("utf-8", errors="replace")
        assignments = cls._UNIT_LINE.findall(text)
        if not assignments or not assignments[-1]:
            return _Reading()  # none, or empty: systemd resets to its own default
        value = assignments[-1]
        if value == "infinity":
            return _Reading(math.inf)
        seconds = cls._timespan(value)
        if seconds is None:
            return _Reading(problem=StopTimeoutState.UNRECOGNIZED, detail=value[:40])
        return _Reading(seconds)

    @classmethod
    def _timespan(cls, text: str) -> float | None:
        """Return a systemd time span in seconds, ``None`` if it is not one."""
        lowered = text.lower()
        if not cls._SPAN.fullmatch(lowered):
            return None
        total = 0.0
        for number, unit in cls._TERM.findall(lowered):
            factor = cls._UNITS.get(unit)
            if factor is None:
                return None
            total += float(number) * factor
        return total

    def _installed_budget(self) -> float | None:
        """Return ``INGEST_DRAIN_TIMEOUT_S`` as the installed definition sets it."""
        try:
            raw = self._environment_value(ShutdownPolicy.ENV_NAME)
            value = float(raw) if raw is not None else None
        except (OSError, ValueError, *_PLIST_ERRORS):
            return None
        usable = value is not None and math.isfinite(value) and value > 0
        return value if usable else None

    def _environment_value(self, name: str) -> str | None:
        """Return *name* from the plist's environment or the env file, if set."""
        if self._path.suffix == ".plist":
            env = plistlib.loads(self._path.read_bytes()).get("EnvironmentVariables")
            return env.get(name) if isinstance(env, dict) else None
        if self._env_file is None:
            return None
        pattern = re.compile(rf'^{name}="?([^"\n]*)"?$', re.MULTILINE)
        match = pattern.search(self._env_file.read_text())
        return match.group(1) if match else None
