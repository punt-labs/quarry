"""Registry-backed doctor checks: directory health, enable status."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.results import CheckResult

if TYPE_CHECKING:
    from quarry.sync_registry import DirectoryRegistration


@final
class SyncDiagnostics:
    """Doctor checks over the sync registry: directory health, enable state.

    Each check reads the registry, tolerates a missing or broken one with a
    fail-closed ``CheckResult`` rather than an exception, and never leaks a
    connection.  These moved out of ``doctor.py`` so the diagnostics god module
    no longer owns registry access directly.
    """

    __slots__ = ()

    @staticmethod
    def directories(registry_path: Path) -> CheckResult:
        """Verify registered sync directories still exist on disk."""
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        if not registry_path.exists():
            return SyncDiagnostics._no_registrations()
        try:
            with contextlib.closing(SyncRegistry(registry_path)) as conn:
                return SyncDiagnostics._directories_result(conn.list_registrations())
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name="Sync directories",
                passed=False,
                message=f"registry error: {exc}",
                required=False,
            )

    @staticmethod
    def _no_registrations() -> CheckResult:
        return CheckResult(
            name="Sync directories",
            passed=True,
            message="no registrations",
            required=False,
        )

    @staticmethod
    def _directories_result(regs: list[DirectoryRegistration]) -> CheckResult:
        """Turn a registration list into a pass/fail directory-existence result."""
        if not regs:
            return SyncDiagnostics._no_registrations()
        missing = [reg.collection for reg in regs if not Path(reg.directory).is_dir()]
        if missing:
            names = ", ".join(missing[:3])
            return CheckResult(
                name="Sync directories",
                passed=False,
                message=f"{len(missing)} missing: {names}",
                required=False,
            )
        return CheckResult(
            name="Sync directories",
            passed=True,
            message=f"{len(regs)} directories OK",
            required=False,
        )

    @staticmethod
    def enable_status(registry_path: Path, cwd: str) -> CheckResult:
        """Report whether *cwd* has quarry enabled and its config is present."""
        from quarry.collection_resolver import CollectionResolver  # noqa: PLC0415
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        conn = SyncRegistry(registry_path)
        try:
            collection = (
                CollectionResolver(conn).covering_collection(cwd) if cwd else None
            )
        finally:
            conn.close()
        if collection is None:
            return CheckResult(
                name="Enable status",
                passed=False,
                message="not enabled -- run 'quarry enable'",
                required=False,
            )
        captures = f"{collection}-captures"
        config_path = Path(cwd) / ".punt-labs" / "quarry" / "config.md"
        config_exists = config_path.is_file()
        parts = [f"collection: {collection}, captures: {captures}"]
        if not config_exists:
            parts.append("config.md missing (run 'quarry enable')")
        return CheckResult(
            name="Enable status",
            passed=config_exists,
            message=", ".join(parts),
            required=False,
        )
