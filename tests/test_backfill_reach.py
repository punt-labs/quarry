"""Tests for quarry.backfill_reach: which sessions a backfill can recover."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.backfill_reach import BackfillReach
from quarry.config import Settings
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path


def register_for_backfill(directory: Path) -> None:
    """Register *directory* in the registry a hook and a backfill both read."""
    registry = SyncRegistry(Settings.load().resolve_db_paths(None).registry_path)
    try:
        # The registry outlives a test, so the name must be as unique as the path.
        registry.register_directory(directory, f"reach-{directory.name}")
    finally:
        registry.close()


class TestIncludes:
    def test_a_registered_directory_is_reached(self, tmp_path: Path) -> None:
        register_for_backfill(tmp_path)
        assert BackfillReach.includes(str(tmp_path))

    def test_an_unregistered_directory_is_not(self, tmp_path: Path) -> None:
        assert not BackfillReach.includes(str(tmp_path))

    def test_a_subdirectory_of_a_registered_one_is_not(self, tmp_path: Path) -> None:
        # Its transcripts live in a project directory backfill never opens.
        register_for_backfill(tmp_path)
        child = tmp_path / "pkg"
        child.mkdir()
        assert not BackfillReach.includes(str(child))

    def test_a_symlink_to_a_registered_directory_is_reached(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "real"
        real.mkdir()
        register_for_backfill(real)
        link = tmp_path / "link"
        link.symlink_to(real)
        assert BackfillReach.includes(str(link))

    def test_an_empty_cwd_is_not(self) -> None:
        assert not BackfillReach.includes("")

    def test_an_unreadable_registry_is_not(self, tmp_path: Path) -> None:
        register_for_backfill(tmp_path)
        with patch.object(SyncRegistry, "list_registrations", side_effect=OSError):
            assert not BackfillReach.includes(str(tmp_path))
