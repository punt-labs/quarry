"""The ``register`` and ``deregister`` commands: dispatch a directory's registration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import DeregisterRequest, RegisterRequest
from quarry.collection_namer import CollectionNamer

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class DirectoryCli:
    """Serve ``register`` and ``deregister``: fire-and-forget calls to the daemon."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``register`` and ``deregister``."""
        app.command(name="register")(self._p.cli_errors(self._register))
        app.command(name="deregister")(self._p.cli_errors(self._deregister))

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
        resolved_path = directory.expanduser().resolve()
        resolved = str(resolved_path)
        # The leaf, sanitized as CollectionNamer does for every auto-picked name
        # (the daemon refuses any other); "root" for a leaf with nothing left.
        col = collection or CollectionNamer.safe(resolved_path.name) or "root"
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
