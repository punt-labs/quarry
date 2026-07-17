"""The directory-sync commands: ``sync``, ``register``, ``deregister``, ``status``.

All pure client calls.  ``deregister`` fires the daemon's purge task and waits
for the deleted-chunk count so its ``{collection, removed, deleted_chunks}`` shape
is identical to what the local path emitted (bug-class-3 parity); a 409 "already
in progress" on ``sync`` is mapped to exit 0 by the shared error decorator.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import DeregisterRequest, RegisterRequest
from quarry.deregister_result import DeregisterResult
from quarry.formatting import format_status

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing
    from quarry.client import TaskOutcome


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
        """Sync all registered directories: ingest new/changed, remove deleted."""
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
        """Register a directory for incremental sync."""
        # The daemon enforces an absolute path inside its $HOME, so send the
        # resolved path the server will allowlist against.
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
        """Remove a directory registration. Optionally keep indexed data."""
        client = self._p.client()
        accepted = client.deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        )
        outcome = client.await_task(accepted.task_id)
        if not outcome.is_completed:
            self._p.err_console.print(
                f"Deregister purge did not complete: {outcome.error or outcome.status}"
                f" (the registration was removed; run 'quarry status' to verify).",
                style="red",
            )
            raise typer.Exit(code=1)
        result = DeregisterResult(
            collection, accepted.removed, self._deleted_chunks(outcome)
        )
        self._p.emit(
            result.as_dict(),
            f"Deregistered collection {collection!r} "
            f"({result.removed} files, {result.deleted_chunks} chunks deleted)",
        )

    def _status(self) -> None:
        """Show database status: documents, chunks, storage, model info."""
        resp = self._p.client().status()
        data = resp.model_dump()
        self._p.emit(data, format_status(data))

    @staticmethod
    def _deleted_chunks(outcome: TaskOutcome) -> int:
        """Return the purge task's deleted-chunk count (a wire integer)."""
        value = outcome.results.get("deleted_chunks")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
