"""Behaviour of the ``quarry missions`` command group (MissionsCli)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.mission_sync_types import MissionSyncOutcome
from tests.mission_fixtures import repo_with_missions

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_OUTCOME = MissionSyncOutcome(
    filed=("mission-quarry-m-1-r1",), skipped=(), errors=(), dry_run=False
)
_FAILED = MissionSyncOutcome(
    filed=(), skipped=(), errors=("m-2: contract.yaml is not a mapping",), dry_run=False
)


@pytest.fixture
def in_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = repo_with_missions(tmp_path / "quarry")
    monkeypatch.chdir(repo)
    return repo


class TestSync:
    def test_json_shape(self, in_repo: Path) -> None:
        with (
            patch(
                "quarry.cli_missions.MissionMemorySync.for_repo", return_value=_OUTCOME
            ),
            patch("quarry.__main__.TargetResolver.connect"),
        ):
            result = runner.invoke(app, ["--json", "missions", "sync"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == _OUTCOME.to_dict()

    def test_text_output_renders_the_outcome(self, in_repo: Path) -> None:
        with (
            patch(
                "quarry.cli_missions.MissionMemorySync.for_repo", return_value=_OUTCOME
            ),
            patch("quarry.__main__.TargetResolver.connect"),
        ):
            result = runner.invoke(app, ["missions", "sync"])
        assert result.exit_code == 0
        assert "▶  Mission memories: filed 1" in result.output
        assert "filed: mission-quarry-m-1-r1" in result.output

    def test_errors_exit_one(self, in_repo: Path) -> None:
        with (
            patch(
                "quarry.cli_missions.MissionMemorySync.for_repo", return_value=_FAILED
            ),
            patch("quarry.__main__.TargetResolver.connect"),
        ):
            result = runner.invoke(app, ["missions", "sync"])
        assert result.exit_code == 1
        assert "error: m-2: contract.yaml is not a mapping" in result.output

    def test_unknown_mission_id_exits_one(self, in_repo: Path) -> None:
        """End to end through the real store: a missing --mission is not 'filed 0'."""
        with patch("quarry.__main__.TargetResolver.connect"):
            result = runner.invoke(
                app, ["missions", "sync", "--mission", "no-such-mission", "--dry-run"]
            )
        assert result.exit_code == 1
        assert "would file 0, skipped 0, errors 1" in result.output
        assert "error: mission no-such-mission not found under" in result.output

    def test_contract_less_directory_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through the real store: a log-only directory is not an error."""
        repo = repo_with_missions(tmp_path / "quarry", mission_ids=())
        stray = repo / ".punt-labs" / "ethos" / "missions" / "m-2026-09-30-003"
        stray.mkdir()
        (stray / "log-3f2a9c1e-0000-4000-8000-000000000000-1-1.jsonl").write_text(
            '{"event": "delegated"}\n'
        )
        monkeypatch.chdir(repo)
        with patch("quarry.__main__.TargetResolver.connect"):
            result = runner.invoke(app, ["missions", "sync", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "would file 0, skipped 0, errors 0" in result.output

    def test_options_reach_the_sync(self, in_repo: Path) -> None:
        with (
            patch(
                "quarry.cli_missions.MissionMemorySync.for_repo", return_value=_OUTCOME
            ) as sync,
            patch("quarry.__main__.TargetResolver.connect"),
        ):
            result = runner.invoke(
                app, ["missions", "sync", "--mission", "m-1", "--dry-run", "--force"]
            )
        assert result.exit_code == 0, result.output
        options = sync.call_args[0][2]
        assert options.mission_id == "m-1"
        assert options.dry_run is True
        assert options.force is True

    def test_cwd_is_the_scanned_repo(self, in_repo: Path) -> None:
        with (
            patch(
                "quarry.cli_missions.MissionMemorySync.for_repo", return_value=_OUTCOME
            ) as sync,
            patch("quarry.__main__.TargetResolver.connect"),
        ):
            runner.invoke(app, ["missions", "sync"])
        assert sync.call_args[0][0] == in_repo


class TestGroup:
    def test_bare_group_demands_a_subcommand(self) -> None:
        result = runner.invoke(app, ["missions"])
        assert result.exit_code == 1
        assert "specify a subcommand" in result.output

    def test_help_lists_sync_and_its_flags(self) -> None:
        result = runner.invoke(app, ["missions", "sync", "--help"])
        assert result.exit_code == 0
        for flag in ("--mission", "--dry-run", "--force"):
            assert flag in result.output

    def test_top_level_help_lists_missions(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert "missions" in result.output
