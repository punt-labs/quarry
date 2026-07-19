"""The directory-sync commands: ``sync``, ``register``, ``deregister``, ``status``.

All pure client calls against the daemon that owns the registry (DES-031 I2): the
CLI never touches a local ``SyncRegistry``.  The task-dispatching commands are
fire-and-forget (DES-001) — the daemon validates synchronously before the 202
(malformed body / 409-concurrent for sync; path guard for register; existence +
404 for deregister), so a rejection still exits non-zero via the shared decorator,
and only the index/purge processing is deferred.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import DeregisterRequest, RegisterRequest
from quarry.formatting import format_status

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class SyncCli:
    """Serve ``sync``/``register``/``deregister``/``status`` on an injected plumbing."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``sync``, ``register``, ``deregister``, and ``status`` to *app*."""
        app.command(name="sync")(self._p.cli_errors(self._sync))
        app.command(name="register")(self._p.cli_errors(self._register))
        app.command(name="deregister")(self._p.cli_errors(self._deregister))
        app.command(name="status")(self._p.cli_errors(self._status))

    def _sync(
        self,
        workers: Annotated[
            int | None,
            typer.Option("--workers", "-w", help="Ignored — the daemon sizes workers"),
        ] = None,
    ) -> None:
        """Sync all registered directories (dispatch only).

        Returns the task id; poll it with ``quarry status``.  A 409 "already in
        progress" raised by the daemon is mapped to exit 0 by the decorator.
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

    def _register(
        self,
        directory: Annotated[Path, typer.Argument(help="Directory to register")],
        collection: Annotated[
            str,
            typer.Option("--collection", "-c", help="Collection name (default: dir)"),
        ] = "",
    ) -> None:
        """Register a directory for incremental sync (dispatch only).

        The path is resolved against $HOME and re-guarded by the daemon on its own
        filesystem (traversal + $HOME allowlist); the registry write is deferred.
        """
        resolved = str(directory.expanduser().resolve())
        col = collection or directory.name or Path(resolved).name
        accepted = self._p.client().register(
            RegisterRequest(directory=resolved, collection=col)
        )
        self._p.emit(
            accepted.model_dump(),
            f"Register {accepted.status}: task_id={accepted.task_id}",
        )

    def _deregister(
        self,
        collection: Annotated[str, typer.Argument(help="Collection to deregister")],
        keep_data: Annotated[
            bool, typer.Option("--keep-data", help="Keep indexed data in LanceDB")
        ] = False,
    ) -> None:
        """Remove a directory registration (dispatch only).

        The daemon drops the registry row and reports ``removed`` synchronously
        (a 404 if the collection is unknown); the chunk purge runs as a background
        task, so ``deleted_chunks`` is not awaited here.
        """
        accepted = self._p.client().deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        )
        self._p.emit(
            accepted.model_dump(),
            f"Deregistered collection {collection!r} ({accepted.removed} files); "
            f"chunk purge {accepted.status}: task_id={accepted.task_id}",
        )

    def _status(self) -> None:
        """Show database status: documents, chunks, storage, model info."""
        resp = self._p.client().status()
        data = resp.model_dump()
        self._p.emit(data, format_status(data))
