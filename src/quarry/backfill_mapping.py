"""Claude Code project directories, resolved to their quarry collections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.sync_registry import DirectoryRegistration

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@final
@dataclass(frozen=True, slots=True)
class ProjectMapping:
    """Maps an encoded Claude project directory to a quarry collection."""

    encoded_dir: str
    project_path: str
    collection: str

    @property
    def captures_collection(self) -> str:
        """The sibling collection this project's web-fetch captures write."""
        return f"{self.collection}-captures"

    def transcript_files(self) -> list[Path]:
        """Return all JSONL transcript files for this project, sorted."""
        project_dir = CLAUDE_PROJECTS_DIR / self.encoded_dir
        if not project_dir.is_dir():
            return []
        return sorted(project_dir.glob("*.jsonl"))


@final
class ProjectMappingResolver:
    """Resolves quarry sync registrations against Claude Code's project dirs."""

    @staticmethod
    def encode(project_path: str) -> str:
        """Encode a project path the same way Claude Code does.

        Replace ``/`` with ``-``.  The leading ``-`` is preserved — Claude
        Code keeps it (e.g. ``/Users/jm`` → ``-Users-jm``).
        """
        return project_path.replace("/", "-")

    @staticmethod
    def resolve_all(
        registrations: list[DirectoryRegistration],
    ) -> list[ProjectMapping]:
        """Build mappings from encoded Claude project dirs to quarry collections.

        For each registration, encode its directory path and check whether a
        matching subdirectory exists under ``~/.claude/projects/``. This avoids
        the ambiguous reverse-decode problem (hyphens in directory names).
        """
        mappings: list[ProjectMapping] = []
        existing_dirs = ProjectMappingResolver._project_dir_names()
        for reg in registrations:
            encoded = ProjectMappingResolver.encode(reg.directory)
            if encoded in existing_dirs:
                mappings.append(
                    ProjectMapping(
                        encoded_dir=encoded,
                        project_path=reg.directory,
                        collection=reg.collection,
                    )
                )
        return mappings

    @staticmethod
    def filter_by_project(
        mappings: list[ProjectMapping], project_filter: str | None
    ) -> list[ProjectMapping]:
        """Return only the mapping for *project_filter*; all mappings when unset."""
        if not project_filter:
            return mappings
        return [m for m in mappings if m.project_path == project_filter]

    @staticmethod
    def count_unmapped(mappings: list[ProjectMapping]) -> int:
        """Count Claude project directories that have no quarry registration."""
        mapped_dirs = {m.encoded_dir for m in mappings}
        return len(ProjectMappingResolver._project_dir_names() - mapped_dirs)

    @staticmethod
    def _project_dir_names() -> set[str]:
        """Return every directory name under ``~/.claude/projects/``."""
        if not CLAUDE_PROJECTS_DIR.is_dir():
            return set()
        return {d.name for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir()}
