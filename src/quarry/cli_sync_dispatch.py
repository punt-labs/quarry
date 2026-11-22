"""Register and dispatch daemon-backed directory synchronization commands.

The commands are fire-and-forget (DES-001). The daemon validates before the
202 response: malformed body or 409-concurrent for sync, a path guard for
registration, and a 404 for an unknown deregistration. The shared CLI error
boundary handles refusals; only indexing or purging is deferred.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.cli_directories import DirectoryCli

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class DirectorySyncCli:
    """Attach the directory task commands without handling status reports."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        """Bind directory synchronization to the shared CLI plumbing."""
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``sync``, ``register``, and ``deregister``."""
        app.command(name="sync")(self._p.cli_errors(self._sync))
        DirectoryCli(self._p).register(app)

    def _sync(
        self,
        workers: Annotated[
            int | None,
            typer.Option("--workers", "-w", help="Ignored — the daemon sizes workers"),
        ] = None,
    ) -> None:
        """Sync all registered directories (dispatch only).

        Fire-and-forget: returns the daemon's task id and exits immediately (no
        per-task poll command exists yet — that is the future ``--wait``). A 409
        "already in progress" raised by the daemon is mapped to exit 0 by the
        decorator.
        """
        if workers is not None and not self._p.is_quiet():
            self._p.err_console.print(
                "Warning: --workers is ignored; the daemon sizes its own workers.",
                style="yellow",
            )
        accepted = self._p.client().sync()
        self._p.emit(
            accepted.model_dump(), f"Sync {accepted.status}: task_id={accepted.task_id}"
        )
