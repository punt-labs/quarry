"""What doctor's checks depend on, resolved once: the home and the settings."""

from __future__ import annotations

import enum
import logging
from pathlib import Path
from typing import Self, final

from pydantic import ValidationError

from quarry.config import Settings
from quarry.doctor_failure import CheckFailure
from quarry.home_paths import HomeUnresolvedError, QuarryHome
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult

logger = logging.getLogger(__name__)

# Settings checked against this root when the home is missing, for validation only.
_PLACEHOLDER_ROOT = Path("/quarry-root-for-validation-only")


class Need(enum.Flag):
    """What a check depends on before it can say anything true."""

    NONE = 0
    HOME = enum.auto()
    SETTINGS = enum.auto()


@final
class Preconditions:
    """The home directory and the settings, each resolved exactly once.

    Every check that depends on either is run or skipped by what this holds, so
    a missing home or an invalid setting is discovered here, reported by the Home
    and Settings checks (the only places those faults are counted), and never
    again by a check that happens to trip over it.  A check that reads the
    settings reads these.
    """

    __slots__ = ("_home_error", "_invalid", "_settings")

    _home_error: HomeUnresolvedError | None  # None: the home resolved
    _invalid: Exception | None  # None: the settings loaded, or only the home blocks
    _settings: Settings | None  # None: the settings could not be loaded

    def __new__(
        cls,
        home_error: HomeUnresolvedError | None,
        settings: Settings | None,
        invalid: Exception | None,
    ) -> Self:
        self = super().__new__(cls)
        self._home_error = home_error
        self._settings = settings
        self._invalid = invalid
        return self

    @classmethod
    def resolve(cls) -> Self:
        """Resolve the home, then the settings, once each."""
        home_error: HomeUnresolvedError | None = None
        try:
            QuarryHome.home()
        except HomeUnresolvedError as exc:
            home_error = exc
        try:
            return cls(home_error, Settings(), None)
        except HomeUnresolvedError:
            # The home is what blocks the settings; a malformed setting behind it
            # is still found, by validating once more against a placeholder root.
            return cls(home_error, None, cls._placeholder_fault())
        except Exception as exc:
            logger.info(
                "doctor: settings are invalid", exc_info=RedactedFailure.of(exc)
            )
            return cls(home_error, None, exc)

    @staticmethod
    def _placeholder_fault() -> Exception | None:  # None: nothing but the home wrong
        try:
            Settings(quarry_root=_PLACEHOLDER_ROOT)
        except ValidationError as exc:
            return exc
        return None

    @property
    def settings(self) -> Settings:
        """Return the resolved settings; only a check whose needs held asks."""
        if self._settings is None:
            msg = "the settings were not resolved"
            raise RuntimeError(msg)
        return self._settings

    def missing(self, need: Need) -> str | None:  # None: every need is met
        """Return what *need* lacks, as the skipped line words it, or ``None``."""
        if Need.SETTINGS in need and self._invalid is not None:
            return "valid settings"  # named first: it is the fault left to fix
        if Need.HOME in need and self._home_error is not None:
            return "the home directory"
        if Need.SETTINGS in need and self._settings is None:
            return "the home directory"  # only the home stopped them loading
        return None

    def home_check(self) -> CheckResult:
        """Return the Home check's result: the one place a missing home is counted."""
        if self._home_error is None:
            return CheckResult(name="Home directory", passed=True, message="resolved")
        return CheckResult(
            name="Home directory",
            passed=False,
            message=f"cannot be determined ({self._home_error}); "
            "set HOME, or give this user a passwd entry",
        )

    def settings_check(self) -> CheckResult:
        """Return the Settings check's result: the one place invalid settings count."""
        if self._invalid is not None:
            reason = CheckFailure.settings_reason(self._invalid)
            return CheckResult(
                name="Settings", passed=False, message=f"invalid settings ({reason})"
            )
        if self._settings is None:
            return CheckResult(
                name="Settings",
                passed=False,
                message="skipped (needs the home directory)",
                required=False,
            )
        return CheckResult(name="Settings", passed=True, message="valid")
