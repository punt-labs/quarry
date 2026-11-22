"""Exercise directory sync dispatch through its registered Typer command."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from quarry.api import TaskAccepted
from quarry.cli_captures import CliPlumbing
from quarry.cli_sync_dispatch import DirectorySyncCli
from quarry.client import QuarryClient


@pytest.mark.parametrize(
    ("options", "quiet", "warns"),
    [([], False, False), (["-w", "3"], False, True), (["--workers", "3"], True, False)],
)
def test_sync_dispatch_ignores_workers(
    options: list[str], *, quiet: bool, warns: bool
) -> None:
    client = Mock(spec=QuarryClient)
    client.sync.return_value = TaskAccepted(task_id="task-1")
    emit, console = Mock(), Mock()
    plumbing = CliPlumbing(
        emit=emit,
        cli_errors=lambda command: command,
        client=lambda: client,
        err_console=console,
        is_quiet=lambda: quiet,
    )
    app = typer.Typer()
    DirectorySyncCli(plumbing).register(app)

    result = CliRunner().invoke(app, ["sync", *options])

    assert result.exit_code == 0, result.output
    client.sync.assert_called_once_with()
    emit.assert_called_once_with(
        {"task_id": "task-1", "status": "accepted"}, "Sync accepted: task_id=task-1"
    )
    if warns:
        console.print.assert_called_once_with(
            "Warning: --workers is ignored; the daemon sizes its own workers.",
            style="yellow",
        )
    else:
        console.print.assert_not_called()
