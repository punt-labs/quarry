"""Claude Code project directories, resolved to their quarry collections."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.sync_registry import DirectoryRegistration

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@final
@dataclass(frozen=True, slots=True)
class ProjectMapping:
    """Maps an encoded Claude project directory to a quarry collection."""

    encoded_dir: str
    project_path: str
    collection: str
    shared: bool = False

    @property
    def captures_collection(self) -> str:
        """The sibling collection this project's web-fetch captures write."""
        return f"{self.collection}-captures"

    def transcript_files(self) -> list[Path]:
        """Return this project's JSONL transcript files, sorted.

        A ``shared`` dir is one Claude Code's lossy encoding gives to more than
        one registration (``/w/my.notes`` and ``/w/my_notes`` both become
        ``-w-my-notes``).  Its transcripts are filed by the ``cwd`` each records,
        so no session lands in the collection of a project it never ran in; one
        that records no ``cwd`` cannot be attributed and is skipped.
        """
        project_dir = CLAUDE_PROJECTS_DIR / self.encoded_dir
        if not project_dir.is_dir():
            return []
        files = sorted(project_dir.glob("*.jsonl"))
        if not self.shared:
            return files
        mine = Path(self.project_path)
        return [
            f for f in files if (cwd := self._recorded_cwd(f)) and Path(cwd) == mine
        ]

    @staticmethod
    def _recorded_cwd(transcript: Path) -> str | None:
        """Return the first ``cwd`` *transcript* records, or ``None``."""
        with contextlib.suppress(OSError), transcript.open(errors="replace") as lines:
            for line in lines:
                with contextlib.suppress(ValueError):
                    record = json.loads(line)
                    if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                        return str(record["cwd"])
        return None


@final
class ProjectMappingResolver:
    """Resolves quarry sync registrations against Claude Code's project dirs."""

    @staticmethod
    def encode(project_path: str) -> str:
        """Encode a project path the same way Claude Code does.

        Every character but an ASCII letter or digit becomes ``-``, as Claude Code
        names ``~/.claude/projects`` directories: ``/r/.claude/w`` → ``-r--claude-w``.
        """
        return re.sub(r"[^A-Za-z0-9]", "-", project_path)

    @staticmethod
    def resolve_all(
        registrations: list[DirectoryRegistration],
    ) -> list[ProjectMapping]:
        """Build mappings from encoded Claude project dirs to quarry collections.

        For each registration, encode its directory path and check whether a
        matching subdirectory exists under ``~/.claude/projects/``. This avoids
        the ambiguous reverse-decode problem (hyphens in directory names).
        """
        existing_dirs = ProjectMappingResolver._project_dir_names()
        encoded = [
            (reg, ProjectMappingResolver.encode(reg.directory)) for reg in registrations
        ]
        shared = ProjectMappingResolver._shared(
            [name for _, name in encoded if name in existing_dirs]
        )
        return [
            ProjectMapping(
                encoded_dir=name,
                project_path=reg.directory,
                collection=reg.collection,
                shared=name in shared,
            )
            for reg, name in encoded
            if name in existing_dirs
        ]

    @staticmethod
    def _shared(names: list[str]) -> set[str]:
        """Return the encoded dirs more than one registration maps to, warning once."""
        shared = {name for name, count in Counter(names).items() if count > 1}
        for name in sorted(shared):
            logger.warning(
                "backfill: %s is shared by %d registrations; filing its "
                "transcripts by their recorded cwd",
                name,
                names.count(name),
            )
        return shared

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
