"""Run doctor's checks so that none of them can stop the report."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.doctor_captures import CaptureDiagnostics
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.doctor_failure import CheckFailure
from quarry.doctor_memory import MemoryDiagnostics
from quarry.doctor_preconditions import Need
from quarry.doctor_recall import RecallDiagnostics
from quarry.doctor_sync import SyncDiagnostics
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from quarry.doctor_preconditions import Preconditions

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class CheckEntry:
    """One check: its report name, what to run, what it needs, and its weight.

    ``required`` is what a check that *crashes* counts as: the same as the
    result it would have returned, so a crash never downgrades a required check.
    ``needs`` is what must hold before it can say anything true; when it does not,
    the check is reported as skipped, never dropped.  ``call`` returns ``None``
    for a diagnostic that has nothing to say.
    """

    name: str
    # None: the diagnostic has nothing to report
    call: Callable[[], CheckResult | None]
    required: bool = False
    needs: Need = Need.NONE


@final
class CheckRunner:
    """Run checks (and install steps) so that whatever they raise is a result."""

    __slots__ = ()

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
    def run(
        cls, entries: Iterable[CheckEntry], pre: Preconditions
    ) -> list[CheckResult]:
        """Run every entry in order; return the results that have something to say.

        An entry whose needs do not hold is skipped, with a line saying what it
        lacked; a skip is never an issue, because the Home and Settings checks
        are where those faults are counted.
        """
        results = [cls._one(entry, pre) for entry in entries]
        return [result for result in results if result is not None]

    @classmethod
    # None: the entry's diagnostic had nothing to report.
    def _one(cls, entry: CheckEntry, pre: Preconditions) -> CheckResult | None:
        if (lacks := pre.missing(entry.needs)) is not None:
            return CheckResult(
                name=entry.name,
                passed=False,
                message=f"skipped (needs {lacks})",
                required=False,
            )
        return cls.attempt(entry)


@final
class SettingsChecks:
    """The checks that read the resolved settings or the daemon they describe."""

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
    def entries(
        pre: Preconditions, fts: Callable[[Path], CheckResult]
    ) -> list[CheckEntry]:
        """Return the entries that need the settings; *fts* is the index check."""
        both = Need.HOME | Need.SETTINGS
        return [
            CheckEntry(
                "Daemon",
                lambda: DaemonDiagnostics.reachability(pre.settings),
                needs=both,
            ),
            CheckEntry(
                "Serve token",
                lambda: DaemonDiagnostics.serve_token(pre.settings),
                needs=both,
            ),
            CheckEntry(
                "Fd headroom",
                lambda: DaemonDiagnostics.fd_headroom(pre.settings),
                needs=both,
            ),
            CheckEntry(
                "Stop timeout",
                lambda: DaemonDiagnostics.stop_timeout(pre.settings),
                needs=both,
            ),
            *SettingsChecks._settings_entries(pre, fts),
        ]

    @staticmethod
    def _settings_entries(
        pre: Preconditions, fts: Callable[[Path], CheckResult]
    ) -> list[CheckEntry]:
        need = Need.SETTINGS
        return [
            CheckEntry("FTS index", lambda: fts(pre.settings.lancedb_path), needs=need),
            CheckEntry(
                "Sync directories",
                lambda: SyncDiagnostics.directories(pre.settings.registry_path),
                needs=need,
            ),
            CheckEntry(
                "Sync enable",
                lambda: SyncDiagnostics.enable_status(
                    pre.settings.registry_path, SettingsChecks.cwd()
                ),
                needs=need,
            ),
            CheckEntry(
                "Unlinked captures",
                lambda: CaptureDiagnostics.unlinked(
                    pre.settings.registry_path, pre.settings.lancedb_path
                ),
                needs=need,
            ),
            CheckEntry(
                "Shadow repo",
                lambda: CaptureDiagnostics.shadow_repo(SettingsChecks.cwd()),
                required=True,  # an unverifiable public repo is not "no leak"
            ),
            CheckEntry(
                "Memory corpus",
                lambda: MemoryDiagnostics.corpus(pre.settings.lancedb_path),
                needs=need,
            ),
            CheckEntry(
                "Memory identity",
                lambda: MemoryDiagnostics.identity_active(
                    SettingsChecks.cwd(), pre.settings.lancedb_path
                ),
                needs=Need.HOME | need,
            ),
            CheckEntry(
                "Recall activity",
                lambda: RecallDiagnostics.recent_activity(
                    pre.settings.telemetry_path,
                    telemetry_enabled=pre.settings.telemetry_enabled,
                ),
                needs=need,
            ),
        ]
