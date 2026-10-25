"""RegisteredProjects: the registrations, read once, as the projects backfill scans."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.backfill_mapping import ProjectMappingResolver
from quarry.registered_projects import RegisteredProjects
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path


def _registry(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "registry.db"
    conn = SyncRegistry(path)
    try:
        for name in names:
            directory = tmp_path / name
            directory.mkdir()
            conn.register_directory(directory, name)
    finally:
        conn.close()
    return path


def test_a_claude_project_no_registration_covers_is_counted_not_scanned(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "claude-projects"
    (claude / "-some-other-project").mkdir(parents=True)
    (claude / "-yet-another").mkdir()
    registry = _registry(tmp_path, "alpha")

    with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", claude):
        projects = RegisteredProjects(registry)
        unregistered = projects.unregistered

    assert unregistered == 2
    assert projects.filtered(None) == []


def test_the_registry_is_read_once_and_closed(tmp_path: Path) -> None:
    path = _registry(tmp_path, "alpha")

    with patch.object(SyncRegistry, "close") as close:
        RegisteredProjects(path)

    close.assert_called_once()


def test_the_filter_narrows_the_mapped_projects(tmp_path: Path) -> None:
    path = _registry(tmp_path, "alpha")
    projects = RegisteredProjects(path)

    with patch.object(ProjectMappingResolver, "filter_by_project") as narrowed:
        projects.filtered("alpha")

    narrowed.assert_called_once()
    assert narrowed.call_args.args[1] == "alpha"
