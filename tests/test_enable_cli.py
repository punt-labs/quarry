"""CLI integration tests for quarry enable/disable -- T21 through T25."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.api import TaskAccepted
from quarry.client import TargetResolver, TaskOutcome
from quarry.config import Settings
from quarry.sync_registry import SyncRegistry

runner = CliRunner()


def _fake_client() -> MagicMock:
    """A QuarryClient double whose daemon chunk-purge completes with 0 deleted."""
    client = MagicMock()
    client.delete_collection.return_value = TaskAccepted(task_id="t", status="accepted")
    client.await_task.return_value = TaskOutcome.completed("t", {"deleted": 0})
    return client


@contextlib.contextmanager
def _patch_for_cli(tmp_path: Path) -> Generator[MagicMock]:
    """Patch settings, ethos identities, and the client for CLI isolation."""
    settings = MagicMock()
    settings.registry_path = tmp_path / "registry.db"
    settings.lancedb_path = tmp_path / "lancedb"

    # Ensure registry DB exists.
    conn = SyncRegistry(settings.registry_path)
    conn.close()

    mock_loaded = MagicMock()
    mock_loaded.resolve_db_paths.return_value = settings

    with (
        patch.object(Settings, "load", return_value=mock_loaded),
        patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
        patch.object(TargetResolver, "connect", return_value=_fake_client()),
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


class TestDisablePurgeFailureExitsNonZero:
    @pytest.mark.parametrize(
        "outcome",
        [
            TaskOutcome.failed("t", "daemon purge blew up"),
            TaskOutcome.timed_out("t"),
            TaskOutcome.unreachable("t", "server gone"),
        ],
    )
    def test_incomplete_purge_exits_1(
        self, tmp_path: Path, outcome: TaskOutcome
    ) -> None:
        # A purge that fails, times out, or leaves the daemon unreachable must
        # NOT report success while the chunks remain — disable exits 1, not 0.
        project = tmp_path / "myproject"
        project.mkdir()

        failing = MagicMock()
        failing.delete_collection.return_value = TaskAccepted(
            task_id="t", status="accepted"
        )
        failing.await_task.return_value = outcome

        with _patch_for_cli(tmp_path):
            enable_result = runner.invoke(app, ["enable", str(project)])
            assert enable_result.exit_code == 0, enable_result.output
            with patch.object(TargetResolver, "connect", return_value=failing):
                result = runner.invoke(app, ["disable", str(project)])

        assert result.exit_code == 1, result.output
        assert "did not complete" in result.output


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


class TestJsonErrorPathKeepsStdoutEmpty:
    def test_enable_failure_emits_no_json_to_stdout(self, tmp_path: Path) -> None:
        # A failure in --json mode must not print a JSON error object to stdout;
        # it goes through _cli_errors (stderr only), so `quarry enable --json | jq`
        # never sees a spurious object.
        project = tmp_path / "p"
        project.mkdir()
        with patch(
            "quarry.enable.enable_project",
            side_effect=ValueError("no registration covers"),
        ):
            result = runner.invoke(app, ["--json", "enable", str(project)])
        assert result.exit_code == 1
        assert '"error"' not in result.output

    def test_disable_failure_emits_no_json_to_stdout(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        project.mkdir()
        with patch(
            "quarry.enable.disable_project",
            side_effect=ValueError("no registration covers"),
        ):
            result = runner.invoke(app, ["--json", "disable", str(project)])
        assert result.exit_code == 1
        assert '"error"' not in result.output


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
        conn = SyncRegistry(settings.registry_path)
        conn.register_directory(parent, "project")
        conn.close()

        mock_loaded = MagicMock()
        mock_loaded.resolve_db_paths.return_value = settings

        with (
            patch.object(Settings, "load", return_value=mock_loaded),
            patch("quarry.enable._GLOBAL_IDENTITIES", tmp_path / "no-ethos"),
        ):
            result = runner.invoke(app, ["enable", str(child)])

        assert result.exit_code == 1
        assert "already covered" in result.output
