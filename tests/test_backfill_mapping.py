"""Tests for quarry.backfill_mapping — ProjectMappingResolver's set arithmetic.

These give ``count_unmapped`` and ``filter_by_project`` direct unit coverage,
closing the gap where they were previously only reached transitively through
``backfill_sessions`` in ``tests/test_backfill.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from quarry.backfill_mapping import ProjectMapping, ProjectMappingResolver


class TestCountUnmapped:
    """count_unmapped: project dirs on disk minus the mapped encoded_dir set."""

    def test_counts_dirs_with_no_matching_mapping(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        (projects_dir / "mapped-project").mkdir(parents=True)
        (projects_dir / "unmapped-project").mkdir(parents=True)
        mappings = [ProjectMapping("mapped-project", "/p", "p")]

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            count = ProjectMappingResolver.count_unmapped(mappings)

        assert count == 1

    def test_zero_when_every_dir_is_mapped(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / ".claude" / "projects"
        (projects_dir / "mapped-project").mkdir(parents=True)
        mappings = [ProjectMapping("mapped-project", "/p", "p")]

        with patch("quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", projects_dir):
            count = ProjectMappingResolver.count_unmapped(mappings)

        assert count == 0

    def test_zero_when_projects_dir_is_absent(self, tmp_path: Path) -> None:
        with patch(
            "quarry.backfill_mapping.CLAUDE_PROJECTS_DIR", tmp_path / "does-not-exist"
        ):
            count = ProjectMappingResolver.count_unmapped([])

        assert count == 0


class TestFilterByProject:
    """filter_by_project: select one project_path; empty filter keeps all."""

    _mappings: ClassVar[list[ProjectMapping]] = [
        ProjectMapping("a-dir", "/repo/a", "a"),
        ProjectMapping("b-dir", "/repo/b", "b"),
    ]

    def test_no_filter_returns_all_mappings(self) -> None:
        result = ProjectMappingResolver.filter_by_project(self._mappings, "")
        assert result == self._mappings

    def test_filter_selects_the_matching_project(self) -> None:
        result = ProjectMappingResolver.filter_by_project(self._mappings, "/repo/b")
        assert result == [self._mappings[1]]

    def test_filter_matching_nothing_returns_empty(self) -> None:
        result = ProjectMappingResolver.filter_by_project(self._mappings, "/repo/z")
        assert result == []
