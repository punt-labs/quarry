"""Run doctor's checks so that none of them can stop the report."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.doctor_captures import CaptureDiagnostics
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.doctor_disk import FootprintCheck, FreeDiskCheck
from quarry.doctor_fts import FtsIndexCheck
from quarry.doctor_memory import MemoryDiagnostics
from quarry.doctor_preconditions import Need
from quarry.doctor_recall import RecallDiagnostics
from quarry.doctor_sync import SyncDiagnostics
from quarry.results import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.config import Settings
    from quarry.doctor_preconditions import Preconditions


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

    @classmethod
    def entries(cls, pre: Preconditions) -> list[CheckEntry]:
        """Return the entries that need the settings."""
        return [*cls._daemon_entries(pre), *cls._settings_entries(pre)]

    @classmethod
    def _daemon_entries(cls, pre: Preconditions) -> list[CheckEntry]:
        both = Need.HOME | Need.SETTINGS
        daemon_checks: tuple[tuple[str, Callable[[Settings], CheckResult]], ...] = (
            ("Daemon", DaemonDiagnostics.reachability),
            ("Serve token", DaemonDiagnostics.serve_token),
            ("Fd headroom", DaemonDiagnostics.fd_headroom),
            ("Finalize", DaemonDiagnostics.finalize),
            ("Stop timeout", DaemonDiagnostics.stop_timeout),
        )
        return [
            CheckEntry(name, cls._on_settings(check, pre), needs=both)
            for name, check in daemon_checks
        ]

    @staticmethod
    def _on_settings(
        check: Callable[[Settings], CheckResult], pre: Preconditions
    ) -> Callable[[], CheckResult]:
        """Defer reading ``pre.settings`` until the entry runs, after its gate."""
        return lambda: check(pre.settings)

    @staticmethod
    def _on_database(
        check: Callable[[Path], CheckResult], pre: Preconditions
    ) -> CheckResult:
        """Run *check* on the database path, read only after the entry's gate."""
        return check(pre.settings.lancedb_path)

    @classmethod
    def _settings_entries(cls, pre: Preconditions) -> list[CheckEntry]:
        need = Need.SETTINGS
        database_checks: tuple[tuple[str, Callable[[Path], CheckResult]], ...] = (
            ("FTS index", FtsIndexCheck.check),
            ("Disk footprint", FootprintCheck.check),
            ("Free disk", FreeDiskCheck.check),
        )
        return [
            *(
                CheckEntry(name, partial(cls._on_database, check, pre), needs=need)
                for name, check in database_checks
            ),
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
                partial(cls._on_database, MemoryDiagnostics.corpus, pre),
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
