"""Tests for quarry.cli_directories: the register and deregister commands.

Exercised in isolation with a stub CliPlumbing whose ``client`` factory returns a
fake QuarryClient, the same pattern as ``test_cli_sync.py``.  The wiring into the
real app is covered by the ``--help`` tests below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import typer
from typer.testing import CliRunner

from quarry.__main__ import app as real_app
from quarry.api import DeregisterAccepted, RegisterRequest, TaskAccepted
from quarry.cli_captures import CliPlumbing
from quarry.cli_directories import DirectoryCli

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from quarry.client import QuarryClient

runner = CliRunner()


def _app(client: MagicMock, lines: list[str]) -> typer.Typer:
    def emit(_data: object, text: str = "") -> None:
        lines.append(text)

    def cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
        return fn

    plumbing = CliPlumbing(
        emit=emit,
        cli_errors=cli_errors,
        client=lambda: cast("QuarryClient", client),
        err_console=MagicMock(),
        is_quiet=lambda: False,
    )
    app = typer.Typer()
    DirectoryCli(plumbing).register(app)
    return app


def _registered_as(tmp_path: Path, leaf: str, *args: str) -> RegisterRequest:
    directory = tmp_path / leaf
    directory.mkdir()
    client = MagicMock()
    client.register.return_value = TaskAccepted(task_id="t1")
    result = runner.invoke(_app(client, []), ["register", str(directory), *args])
    assert result.exit_code == 0, result.output
    request: RegisterRequest = client.register.call_args.args[0]
    return request


def test_register_and_deregister_are_commands_of_the_app() -> None:
    for command in ("register", "deregister"):
        result = runner.invoke(real_app, [command, "--help"])
        assert result.exit_code == 0, result.output


def test_register_says_what_the_default_collection_is() -> None:
    result = runner.invoke(real_app, ["register", "--help"])
    assert "default: dir" in result.output


class TestRegister:
    def test_the_default_collection_is_the_directory_leaf(self, tmp_path: Path) -> None:
        assert _registered_as(tmp_path, "notes").collection == "notes"

    def test_a_leaf_with_a_space_is_sanitized(self, tmp_path: Path) -> None:
        assert _registered_as(tmp_path, "My Guide").collection == "My-Guide"

    def test_a_leaf_with_nothing_left_falls_back_to_root(self, tmp_path: Path) -> None:
        assert _registered_as(tmp_path, "...").collection == "root"

    def test_an_explicit_collection_is_used_as_given(self, tmp_path: Path) -> None:
        request = _registered_as(tmp_path, "notes", "--collection", "mine")
        assert request.collection == "mine"

    def test_the_directory_is_sent_resolved(self, tmp_path: Path) -> None:
        assert _registered_as(tmp_path, "notes").directory == str(
            (tmp_path / "notes").resolve()
        )

    def test_the_output_line_names_the_task(self, tmp_path: Path) -> None:
        directory = tmp_path / "notes"
        directory.mkdir()
        client, lines = MagicMock(), list[str]()
        client.register.return_value = TaskAccepted(task_id="t1")
        runner.invoke(_app(client, lines), ["register", str(directory)])
        assert lines == ["Register accepted: task_id=t1"]


class TestDeregister:
    @staticmethod
    def _run(*args: str) -> tuple[MagicMock, list[str]]:
        client = MagicMock()
        lines: list[str] = []
        client.deregister.return_value = DeregisterAccepted(
            status="queued", task_id="t2", removed=3
        )
        result = runner.invoke(_app(client, lines), ["deregister", *args])
        assert result.exit_code == 0, result.output
        return client, lines

    def test_the_output_line_names_the_collection_and_the_purge(self) -> None:
        _, lines = self._run("notes")
        assert lines == [
            "Deregistered collection 'notes' (3 files); chunk purge queued: task_id=t2"
        ]

    def test_data_is_purged_by_default(self) -> None:
        client, _ = self._run("notes")
        assert client.deregister.call_args.args[0].keep_data is False

    def test_keep_data_is_sent(self) -> None:
        client, _ = self._run("notes", "--keep-data")
        assert client.deregister.call_args.args[0].keep_data is True
