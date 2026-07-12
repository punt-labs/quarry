"""CLI tests for `quarry captures push/init`: local vs remote JSON equivalence
(bug class 3) and the explicit-path non-zero exit on push failure."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.shadow.sync import ShadowSyncResult

runner = CliRunner()

_RESULT = ShadowSyncResult(
    pushed=True, committed=True, rescrubbed=1, aborted_reason="", race_failures=()
)


def _local_env() -> AbstractContextManager[MagicMock]:
    return patch("quarry.__main__._safe_proxy_config", return_value={})


def _remote_env() -> AbstractContextManager[MagicMock]:
    return patch(
        "quarry.__main__._safe_proxy_config",
        return_value={"quarry": {"url": "https://h:8420"}},
    )


class TestPushEquivalence:
    def test_local_and_remote_json_field_names_match(self) -> None:
        local_settings = MagicMock()
        with (
            _local_env(),
            patch("quarry.__main__._resolved_settings", return_value=local_settings),
            patch(
                "quarry.shadow.CaptureSync.push_registered",
                return_value={"proj": _RESULT},
            ),
        ):
            local = runner.invoke(app, ["--json", "captures", "push"])
        assert local.exit_code == 0
        local_json = json.loads(local.stdout)

        remote_payload = {"results": {"proj": _RESULT.to_dict()}}
        with (
            _remote_env(),
            patch("quarry.__main__.RemoteClient") as remote_client,
        ):
            remote_client.return_value.request.return_value = remote_payload
            remote = runner.invoke(app, ["--json", "captures", "push"])
        assert remote.exit_code == 0
        remote_json = json.loads(remote.stdout)

        # Identical envelope and per-project field names across surfaces.
        assert set(local_json) == set(remote_json) == {"results"}
        assert set(local_json["results"]["proj"]) == set(remote_json["results"]["proj"])


class TestPushExitCode:
    def test_nonzero_exit_when_a_project_not_pushed(self) -> None:
        aborted = ShadowSyncResult.aborted("public-remote")
        with (
            _local_env(),
            patch("quarry.__main__._resolved_settings", return_value=MagicMock()),
            patch(
                "quarry.shadow.CaptureSync.push_registered",
                return_value={"proj": aborted},
            ),
        ):
            result = runner.invoke(app, ["captures", "push"])
        assert result.exit_code == 1


class TestInit:
    def test_init_reports_no_config(self, tmp_path: Path) -> None:
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=None),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(app, ["captures", "init"])
        assert result.exit_code == 1
        assert "shadow" in result.stdout.lower()

    def test_init_bootstraps(self, tmp_path: Path) -> None:
        shadow = MagicMock()
        shadow.bootstrap.return_value = True
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=shadow),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(app, ["captures", "init"])
        assert result.exit_code == 0
        shadow.bootstrap.assert_called_once_with(create=False)

    def test_init_create_flag(self, tmp_path: Path) -> None:
        shadow = MagicMock()
        shadow.bootstrap.return_value = True
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=shadow),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            runner.invoke(app, ["captures", "init", "--create"])
        shadow.bootstrap.assert_called_once_with(create=True)


class TestCapturesCallback:
    def test_no_subcommand_errors(self) -> None:
        result = runner.invoke(app, ["captures"])
        assert result.exit_code == 1
