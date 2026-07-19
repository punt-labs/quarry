"""Tests for the extracted `quarry captures` command group (CapturesCli).

Exercised in isolation with a stub CliPlumbing whose ``client`` factory returns a
fake QuarryClient — no __main__ patching — plus one smoke test confirming the
group is wired onto the top-level app.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quarry.api import CapturesPushResponse
from quarry.cli_captures import CapturesCli, CliPlumbing
from quarry.shadow.sync import ShadowSyncResult

if TYPE_CHECKING:
    from collections.abc import Callable

    import typer

    from quarry.client import QuarryClient

runner = CliRunner()

_PUSHED = ShadowSyncResult(
    pushed=True, committed=True, rescrubbed=1, aborted_reason="", race_failures=()
)


def _plumbing(*, client: object, recorder: list[object]) -> CliPlumbing:
    def emit(data: object, _text: str = "") -> None:
        recorder.append(data)

    def cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
        return fn

    return CliPlumbing(
        emit=emit,
        cli_errors=cli_errors,
        client=lambda: cast("QuarryClient", client),
        err_console=MagicMock(),
        is_quiet=lambda: False,
    )


def _app(*, client: object, recorder: list[object]) -> typer.Typer:
    return CapturesCli(_plumbing(client=client, recorder=recorder)).build()


def _client_pushing(results: dict[str, dict[str, object]]) -> MagicMock:
    client = MagicMock()
    client.captures_push.return_value = CapturesPushResponse(results=results)
    return client


class TestPushExitCode:
    def test_nonzero_exit_when_not_pushed(self) -> None:
        aborted = ShadowSyncResult.aborted("public-remote")
        client = _client_pushing({"proj": aborted.to_dict()})
        result = runner.invoke(_app(client=client, recorder=[]), ["push"])
        assert result.exit_code == 1

    def test_success_exits_zero(self) -> None:
        client = _client_pushing({"proj": _PUSHED.to_dict()})
        result = runner.invoke(_app(client=client, recorder=[]), ["push"])
        assert result.exit_code == 0

    def test_emits_results_envelope(self) -> None:
        recorder: list[object] = []
        client = _client_pushing({"proj": _PUSHED.to_dict()})
        runner.invoke(_app(client=client, recorder=recorder), ["push"])
        data = recorder[0]
        assert isinstance(data, dict)
        assert set(data) == {"results"}
        assert set(data["results"]["proj"]) == set(_PUSHED.to_dict())


class TestInit:
    def test_reports_no_config(self, tmp_path: Path) -> None:
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=None),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(_app(client=MagicMock(), recorder=[]), ["init"])
        assert result.exit_code == 1

    def test_bootstraps(self, tmp_path: Path) -> None:
        shadow = MagicMock()
        shadow.bootstrap.return_value = True
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=shadow),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(_app(client=MagicMock(), recorder=[]), ["init"])
        assert result.exit_code == 0
        shadow.bootstrap.assert_called_once_with(create=False)

    def test_create_flag(self, tmp_path: Path) -> None:
        shadow = MagicMock()
        shadow.bootstrap.return_value = True
        with (
            patch("quarry.shadow.CaptureSync.from_directory", return_value=shadow),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            runner.invoke(_app(client=MagicMock(), recorder=[]), ["init", "--create"])
        shadow.bootstrap.assert_called_once_with(create=True)


class TestCallback:
    def test_no_subcommand_errors(self) -> None:
        result = runner.invoke(_app(client=MagicMock(), recorder=[]), [])
        assert result.exit_code == 1


class TestWiring:
    def test_captures_registered_on_main_app(self) -> None:
        from quarry.__main__ import app as main_app

        result = runner.invoke(main_app, ["captures"])
        assert result.exit_code == 1
