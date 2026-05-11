"""CLI integration tests for quarry enable/disable -- T21 through T25."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.sync_registry import open_registry, register_directory

runner = CliRunner()


@contextlib.contextmanager
def _patch_for_cli(tmp_path: Path) -> Generator[MagicMock]:
    """Patch settings, ethos identities, and load_settings for CLI isolation."""
    settings = MagicMock()
    settings.registry_path = tmp_path / "registry.db"
    settings.lancedb_path = tmp_path / "lancedb"

    # Ensure registry DB exists.
    conn = open_registry(settings.registry_path)
    conn.close()

    with (
        patch("quarry.config.resolve_db_paths", return_value=settings),
        patch("quarry.config.load_settings", return_value=MagicMock()),
        patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
    ):
        yield settings


# -----------------------------------------------------------------------
# T21: quarry enable CLI happy path
# -----------------------------------------------------------------------


class TestT21EnableCLIHappyPath:
    def test_enable_happy_path(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["enable", str(project)])

        assert result.exit_code == 0, result.output
        assert "myproject" in result.output


# -----------------------------------------------------------------------
# T22: quarry enable --collection custom CLI
# -----------------------------------------------------------------------


class TestT22EnableCLICollectionOverride:
    def test_enable_with_collection_override(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(
                app, ["enable", str(project), "--collection", "custom"]
            )

        assert result.exit_code == 0, result.output
        assert "custom" in result.output


# -----------------------------------------------------------------------
# T23: quarry disable CLI happy path
# -----------------------------------------------------------------------


class TestT23DisableCLIHappyPath:
    def test_disable_happy_path(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            # Enable first.
            enable_result = runner.invoke(app, ["enable", str(project)])
            assert enable_result.exit_code == 0, enable_result.output

            # Then disable.
            disable_result = runner.invoke(app, ["disable", str(project)])

        assert disable_result.exit_code == 0, disable_result.output
        assert "Disabled" in disable_result.output


# -----------------------------------------------------------------------
# T24: quarry disable on unregistered directory
# -----------------------------------------------------------------------


class TestT24DisableCLIUnregistered:
    def test_disable_unregistered_exits_1(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["disable", str(project)])

        assert result.exit_code == 1
        assert "no registration covers" in result.output


# -----------------------------------------------------------------------
# T25: quarry enable --json outputs structured data
# -----------------------------------------------------------------------


class TestT25EnableCLIJsonOutput:
    def test_enable_json_output(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()

        with _patch_for_cli(tmp_path):
            result = runner.invoke(app, ["--json", "enable", str(project)])

        assert result.exit_code == 0, result.output

        data = json.loads(result.stdout)
        assert "directory" in data
        assert "collection" in data
        assert "captures_collection" in data
        assert "created_registration" in data
        assert data["collection"] == "myproject"
        assert data["captures_collection"] == "myproject-captures"
        assert data["created_registration"] is True


# -----------------------------------------------------------------------
# T3b: CLI enable on child of registered parent exits 1
# -----------------------------------------------------------------------


class TestT3bEnableCLIChildExits1:
    def test_child_of_parent_exits_1(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src"
        child.mkdir()

        settings = MagicMock()
        settings.registry_path = tmp_path / "registry.db"
        settings.lancedb_path = tmp_path / "lancedb"

        # Register parent.
        conn = open_registry(settings.registry_path)
        register_directory(conn, parent, "project")
        conn.close()

        with (
            patch(
                "quarry.config.resolve_db_paths",
                return_value=settings,
            ),
            patch("quarry.config.load_settings", return_value=MagicMock()),
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
        ):
            result = runner.invoke(app, ["enable", str(child)])

        assert result.exit_code == 1
        assert "already covered" in result.output
