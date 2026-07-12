"""Tests for the extracted `quarry captures` command group (CapturesCli).

Exercised in isolation with a stub CliPlumbing — no __main__ patching — plus one
smoke test confirming the group is wired onto the top-level app.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.cli_captures import CapturesCli, CliPlumbing
from quarry.shadow.sync import ShadowSyncResult

if TYPE_CHECKING:
    from collections.abc import Callable

    import typer

runner = CliRunner()

_PUSHED = ShadowSyncResult(
    pushed=True, committed=True, rescrubbed=1, aborted_reason="", race_failures=()
)


def _plumbing(*, proxy: dict[str, object], recorder: list[object]) -> CliPlumbing:
    def emit(data: object, _text: str = "") -> None:
        recorder.append(data)

    def cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
        return fn

    return CliPlumbing(
        emit=emit,
        cli_errors=cli_errors,
        safe_proxy_config=lambda: proxy,
        resolved_settings=MagicMock(),
        err_console=MagicMock(),
    )


def _app(*, proxy: dict[str, object], recorder: list[object]) -> typer.Typer:
    return CapturesCli(_plumbing(proxy=proxy, recorder=recorder)).build()


class TestPushExitCode:
    def test_local_nonzero_exit_when_not_pushed(self) -> None:
        aborted = ShadowSyncResult.aborted("public-remote")
        with patch(
            "quarry.shadow.CaptureSync.push_registered",
            return_value={"proj": aborted},
        ):
            result = runner.invoke(_app(proxy={}, recorder=[]), ["push"])
        assert result.exit_code == 1

    def test_remote_nonzero_exit_when_not_pushed(self) -> None:
        # bug class 3: the remote branch must exit non-zero on a refused push,
        # matching the local branch (it previously always returned 0).
        aborted = ShadowSyncResult.aborted("public-remote")
        with patch("quarry.cli_captures.RemoteClient") as remote_client:
            remote_client.return_value.request.return_value = {
                "results": {"proj": aborted.to_dict()}
            }
            app = _app(proxy={"quarry": {"url": "https://h:8420"}}, recorder=[])
            result = runner.invoke(app, ["push"])
        assert result.exit_code == 1

    def test_success_exits_zero(self) -> None:
        with patch(
            "quarry.shadow.CaptureSync.push_registered",
            return_value={"proj": _PUSHED},
        ):
            result = runner.invoke(_app(proxy={}, recorder=[]), ["push"])
        assert result.exit_code == 0


class TestPushEquivalence:
    def test_local_and_remote_emit_same_field_names(self) -> None:
        local: list[object] = []
        with patch(
            "quarry.shadow.CaptureSync.push_registered",
            return_value={"proj": _PUSHED},
        ):
            runner.invoke(_app(proxy={}, recorder=local), ["push"])

        remote: list[object] = []
        with patch("quarry.cli_captures.RemoteClient") as remote_client:
            remote_client.return_value.request.return_value = {
                "results": {"proj": _PUSHED.to_dict()}
            }
            app = _app(proxy={"quarry": {"url": "https://h"}}, recorder=remote)
            runner.invoke(app, ["push"])

        local_data = local[0]
        remote_data = remote[0]
        assert isinstance(local_data, dict)
        assert isinstance(remote_data, dict)
        assert set(local_data) == set(remote_data) == {"results"}
        assert set(local_data["results"]["proj"]) == set(remote_data["results"]["proj"])


class TestInit:
    def test_reports_no_config(self, tmp_path: Path) -> None:
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=None),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(_app(proxy={}, recorder=[]), ["init"])
        assert result.exit_code == 1

    def test_bootstraps(self, tmp_path: Path) -> None:
        shadow = MagicMock()
        shadow.bootstrap.return_value = True
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=shadow),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(_app(proxy={}, recorder=[]), ["init"])
        assert result.exit_code == 0
        shadow.bootstrap.assert_called_once_with(create=False)

    def test_create_flag(self, tmp_path: Path) -> None:
        shadow = MagicMock()
        shadow.bootstrap.return_value = True
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=shadow),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            runner.invoke(_app(proxy={}, recorder=[]), ["init", "--create"])
        shadow.bootstrap.assert_called_once_with(create=True)


class TestCallback:
    def test_no_subcommand_errors(self) -> None:
        result = runner.invoke(_app(proxy={}, recorder=[]), [])
        assert result.exit_code == 1


class TestWiring:
    def test_captures_registered_on_main_app(self) -> None:
        from quarry.__main__ import app as main_app

        result = runner.invoke(main_app, ["captures"])
        assert result.exit_code == 1
