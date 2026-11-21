"""Translate failures at the CLI boundary into diagnostics and exit codes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

import typer

from quarry.client import HttpError, QuarryConnectionError, QuarryError
from quarry.home_paths import HomeUnresolvedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console

_logger = logging.getLogger(__name__)
_AUTOSTART_HINT = (
    "If quarryd is not running, start it with 'quarry install' (managed) or "
    "'quarryd' (foreground)."
)


@final
class CliErrors:
    """Map CLI failures to diagnostics, user-facing output, and exit codes."""

    __slots__ = ("_console", "_emit")

    _console: Console
    _emit: Callable[[object, str], None]

    def __new__(cls, console: Console, emit: Callable[[object, str], None]) -> Self:
        """Bind the CLI error console and the command result emitter."""
        self = super().__new__(cls)
        self._console = console
        self._emit = emit
        return self

    def run(self, command: str, invoke: Callable[[], None]) -> None:
        """Invoke a command and translate its failures to Typer exits."""
        try:
            invoke()
        except (typer.Exit, HomeUnresolvedError):
            raise
        except QuarryError as exc:
            raise typer.Exit(code=self._report(command, exc)) from exc
        except Exception as exc:
            _logger.exception("Command %s failed", command)
            self._console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc

    def _report(self, command: str, error: QuarryError) -> int:
        if isinstance(error, HttpError) and error.is_conflict:
            self._emit_conflict(error)
            return 0
        if isinstance(error, QuarryConnectionError):
            self._connection_failed(command, error)
        else:
            self._console.print(f"Error: {error.message}", style="red")
        return 1

    def _connection_failed(self, command: str, error: QuarryConnectionError) -> None:
        local = error.is_loopback
        # The CLI already renders the error on stderr.
        _logger.info(
            "CLI operation %s failed: could not connect to %s Quarry daemon",
            command.lstrip("_"),
            "local" if local else "remote",
        )
        self._console.print(f"Error: {error.message}", style="red")
        if local:
            self._console.print(_AUTOSTART_HINT, style="yellow")

    def _emit_conflict(self, error: HttpError) -> None:
        # A 409 returns the existing task ID like the 202 acceptance path so
        # the caller can poll the in-flight task instead of dispatching again.
        text = (
            f"Already in progress (task {error.task_id})"
            if error.task_id
            else f"Already in progress: {error.message}"
        )
        self._emit({"task_id": error.task_id, "status": "in_progress"}, text)
