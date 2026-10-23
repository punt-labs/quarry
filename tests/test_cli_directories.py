"""Tests for quarry.cli_directories: the register and deregister commands."""

from __future__ import annotations

from typer.testing import CliRunner

from quarry.__main__ import app

runner = CliRunner()


def test_register_and_deregister_are_commands_of_the_app() -> None:
    for command in ("register", "deregister"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output


def test_register_says_what_the_default_collection_is() -> None:
    result = runner.invoke(app, ["register", "--help"])
    assert "default: dir" in result.output
