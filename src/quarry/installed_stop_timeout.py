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
from quarry.installed_budget import BudgetState, InstalledBudget
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy
from quarry.stop_timeout_verdict import StopTimeoutState, StopTimeoutVerdict
from quarry.systemd_text import SystemdText

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
    problem: StopTimeoutState | None = None  # None: the file read cleanly
    detail: str = ""


@final
@dataclass(frozen=True, slots=True)
class _Invalid:
    """What an installed definition holds in place of a usable setting."""

    reason: str


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
    definition when that sets one (:meth:`budget`), and from settings only when
    the definition truly does not; an unusable one is reported, not replaced.
    """

    _PLAIN_NUMBER: ClassVar[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?")

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

    def budget(self) -> InstalledBudget:
        """Return the teardown budget the installed definition sets, or why not.

        ``INGEST_DRAIN_TIMEOUT_S`` is the number the stop timeout was computed
        from.  A definition that does not set it is ``ABSENT``, and only then do
        the settings decide; one that sets something unusable (not a plain
        number, outside the settings' own bounds, or unreadable) is ``INVALID``
        with the reason, never silently replaced.  The budget lives in the plist
        on launchd and in the env file on systemd, and that is the file named.
        """
        source = self._budget_file()
        try:
            raw = self._environment_value(ShutdownPolicy.ENV_NAME)
        except FileNotFoundError:
            return InstalledBudget(BudgetState.ABSENT, source)
        except OSError as exc:
            return self._invalid(f"cannot be read ({FailureReason.os_name(exc)})")
        except UnicodeDecodeError:
            return self._invalid("cannot be decoded")
        except _PLIST_ERRORS as exc:
            return self._invalid(f"cannot be parsed ({type(exc).__name__})")
        if isinstance(raw, _Invalid):
            return self._invalid(raw.reason)
        if raw is None:
            return InstalledBudget(BudgetState.ABSENT, source)
        return self._checked(raw)

    def _checked(self, raw: str) -> InstalledBudget:
        """Return *raw* as a budget if it is a plain number inside the bounds."""
        if not self._PLAIN_NUMBER.fullmatch(raw):
            return self._invalid(f"{raw[:40]!r} is not a plain number")
        value = float(raw)
        low, high = ShutdownPolicy.total_bounds()
        if not low <= value <= high:
            return self._invalid(
                f"installed budget {value:g}s is outside {low:g}-{high:g}s"
            )
        return InstalledBudget(BudgetState.VALID, self._budget_file(), value=value)

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
        else:
            logger.info("%s", verdict.message)

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

    @staticmethod
    def _unit(raw: bytes) -> _Reading:
        """Return the unit's stop timeout, or why it cannot be read.

        Read as systemd reads it (:class:`~quarry.systemd_text.SystemdText`):
        ``infinity`` and ``0`` both mean no timeout at all, an empty or missing
        assignment means systemd's own default.
        """
        assigned = SystemdText.stop_timeout(raw.decode("utf-8", errors="replace"))
        if not assigned:
            return _Reading()
        if assigned == "infinity":
            return _Reading(math.inf)
        try:
            seconds = SystemdText.timespan(assigned)
        except ValueError:
            return _Reading(problem=StopTimeoutState.UNRECOGNIZED, detail=assigned[:40])
        return _Reading(math.inf if seconds == 0 else seconds)

    def _budget_file(self) -> Path:
        """Return the file the budget is read from: the plist, else the env file."""
        if self._path.suffix == ".plist" or self._env_file is None:
            return self._path
        return self._env_file

    def _invalid(self, reason: str) -> InstalledBudget:
        """Return an unusable budget for this definition, with *reason*."""
        return InstalledBudget(BudgetState.INVALID, self._budget_file(), reason=reason)

    def _environment_value(self, name: str) -> str | _Invalid | None:
        """Return *name* from the plist's environment or the env file.

        ``None`` when the definition does not set it; an :class:`_Invalid` when
        what it holds is not a string setting.  The last assignment in an env
        file wins, as when systemd reads it.
        """
        if self._path.suffix == ".plist":
            root = plistlib.loads(self._path.read_bytes())
            if not isinstance(root, dict):
                return _Invalid("root is not a dictionary")
            env = root.get("EnvironmentVariables")
            if env is None:
                return None
            if not isinstance(env, dict):
                return _Invalid("EnvironmentVariables is not a dictionary")
            value = env.get(name)
            if value is None or isinstance(value, str):
                return value
            return _Invalid(f"{name} is a {type(value).__name__}, not a string")
        if self._env_file is None:
            return None
        return SystemdText.assignment(self._env_file.read_text(), name)
