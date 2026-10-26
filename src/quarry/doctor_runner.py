"""Run doctor's checks so that none of them can stop the report."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.config import Settings
from quarry.doctor_captures import CaptureDiagnostics
from quarry.doctor_failure import CheckFailure
from quarry.doctor_memory import MemoryDiagnostics
from quarry.doctor_recall import RecallDiagnostics
from quarry.doctor_sync import SyncDiagnostics
from quarry.home_paths import HomeUnresolvedError, QuarryHome
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class CheckEntry:
    """One check: its report name, what to run, and whether its failure is fatal.

    ``required`` is what a check that *crashes* counts as: the same as the
    result it would have returned, so a crash never downgrades a required check.
    ``call`` returns ``None`` for a diagnostic that has nothing to say.
    ``needs_home`` marks a check that reads something under the home directory:
    with no home it is not run, because the Home check already reports that one
    fault and a second line for the same cause would count it twice.
    """

    name: str
    # None: the diagnostic has nothing to report
    call: Callable[[], CheckResult | None]
    required: bool = False
    needs_home: bool = False


@final
class CheckRunner:
    """Run checks (and install steps) so that whatever they raise is a result."""

    __slots__ = ()

    @staticmethod
    def home() -> CheckResult:
        """Return whether the home directory resolves (everything else needs it)."""
        try:
            QuarryHome.home()
        except HomeUnresolvedError as exc:
            return CheckResult(
                name="Home directory",
                passed=False,
                message=f"cannot be determined ({exc}); "
                "set HOME, or give this user a passwd entry",
            )
        return CheckResult(name="Home directory", passed=True, message="resolved")

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
    def run(cls, entries: Iterable[CheckEntry]) -> list[CheckResult]:
        """Run every entry in order; return the results that have something to say."""
        has_home = cls.home().passed
        results = [
            cls.attempt(entry) for entry in entries if has_home or not entry.needs_home
        ]
        return [result for result in results if result is not None]


@final
class SettingsChecks:
    """The checks that read the settings, or the one failed check that says why not."""

    __slots__ = ()

    @staticmethod
    def cwd() -> str:
        """Return the working directory, or say it no longer exists."""
        try:
            return str(Path.cwd())
        except FileNotFoundError as exc:
            msg = "the current directory no longer exists"
            raise RuntimeError(msg) from exc
        except PermissionError as exc:
            msg = "the current directory is not accessible"
            raise RuntimeError(msg) from exc

    @staticmethod
    def is_the_home(failure: BaseException | None) -> bool:  # None: end of the chain
        """Return whether *failure* is, or was raised from, an unresolvable home."""
        while failure is not None:
            if isinstance(failure, HomeUnresolvedError):
                return True
            failure = failure.__cause__ or failure.__context__
        return False

    @staticmethod
    def entries(fts: Callable[[Path], CheckResult]) -> list[CheckEntry]:
        """Return the settings-dependent entries; *fts* is the index check."""
        try:
            settings = Settings()
        except Exception as exc:
            logger.info(
                "doctor: settings are invalid", exc_info=RedactedFailure.of(exc)
            )
            if SettingsChecks.is_the_home(exc):
                return []  # the Home check reports it; the settings are not "invalid"
            failed = CheckResult(
                name="Settings",
                passed=False,
                message=f"invalid settings ({CheckFailure.settings_reason(exc)})",
            )
            return [CheckEntry("Settings", lambda: failed, required=True)]
        db, registry = settings.lancedb_path, settings.registry_path
        return [
            CheckEntry("FTS index", lambda: fts(db)),
            CheckEntry(
                "Sync directories", lambda: SyncDiagnostics.directories(registry)
            ),
            CheckEntry(
                "Sync enable",
                lambda: SyncDiagnostics.enable_status(registry, SettingsChecks.cwd()),
            ),
            CheckEntry(
                "Unlinked captures",
                lambda: CaptureDiagnostics.unlinked(registry, db),
            ),
            CheckEntry(
                "Shadow repo",
                lambda: CaptureDiagnostics.shadow_repo(SettingsChecks.cwd()),
                required=True,  # an unverifiable public repo is not "no leak"
            ),
            CheckEntry("Memory corpus", lambda: MemoryDiagnostics.corpus(db)),
            CheckEntry(
                "Memory identity",
                lambda: MemoryDiagnostics.identity_active(SettingsChecks.cwd(), db),
            ),
            CheckEntry(
                "Recall activity",
                lambda: RecallDiagnostics.recent_activity(
                    settings.telemetry_path,
                    telemetry_enabled=settings.telemetry_enabled,
                ),
            ),
        ]
