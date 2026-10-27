"""The directory-sync commands: ``sync``, ``status``, ``insights``, and (through
:class:`~quarry.cli_directories.DirectoryCli`) ``register`` and ``deregister``.

All pure client calls against the daemon that owns the registry (DES-031 I2): the
CLI never touches a local ``SyncRegistry``.  The task-dispatching commands are
fire-and-forget (DES-001) — the daemon validates synchronously before the 202
(malformed body / 409-concurrent for sync; path guard for register; existence +
404 for deregister), so a rejection still exits non-zero via the shared decorator,
and only the index/purge processing is deferred.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.cli_directories import DirectoryCli
from quarry.formatting import format_insights, format_status

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class SyncCli:
    """Serve ``sync``/``status``/``insights``; attach ``register``/``deregister``."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``sync``/``register``/``deregister``/``status``/``insights``."""
        app.command(name="sync")(self._p.cli_errors(self._sync))
        DirectoryCli(self._p).register(app)
        app.command(name="status")(self._p.cli_errors(self._status))
        app.command(name="insights")(self._p.cli_errors(self._insights))

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

    def _status(self) -> None:
        """Show database status: documents, chunks, storage, model info."""
        resp = self._p.client().status()
        data = resp.model_dump()
        self._p.emit(data, format_status(data))

    def _insights(self) -> None:
        """Show recall telemetry: query volume, latency, and recall breakdowns."""
        resp = self._p.client().insights()
        self._p.emit(resp.model_dump(), format_insights(resp))
