"""The registered directories, as the Claude Code projects backfill can scan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_mapping import ProjectMappingResolver
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.backfill_mapping import ProjectMapping


@final
class RegisteredProjects:
    """Read the registrations once and resolve them to Claude Code's project dirs.

    A project directory of Claude Code's that no registration covers is
    unregistered: it is counted, not scanned.
    """

    __slots__ = ("_mappings",)

    _mappings: list[ProjectMapping]

    def __new__(cls, registry_path: Path) -> Self:
        self = super().__new__(cls)
        conn = SyncRegistry(registry_path)
        try:
            registrations = conn.list_registrations()
        finally:
            conn.close()
        self._mappings = ProjectMappingResolver.resolve_all(registrations)
        return self

    @property
    def unregistered(self) -> int:
        """Return how many Claude Code project directories no registration covers."""
        return ProjectMappingResolver.count_unmapped(self._mappings)

    def filtered(self, project_filter: str | None) -> list[ProjectMapping]:
        """Return the mapped projects matching *project_filter* (all if it is empty)."""
        return ProjectMappingResolver.filter_by_project(self._mappings, project_filter)
