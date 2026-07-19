"""The document-facing commands: ``show``, ``delete``, and the ``list`` group.

All are pure client calls.  ``show`` and the ``list`` nouns read one wire model
each and render it; ``delete`` fires a 202 task and waits for the deleted-chunk
count so the emitted ``{deleted, name, type}`` shape matches what the local path
produced before the daemon owned deletion (bug-class-3 field parity).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import (
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    ShowRequest,
)
from quarry.cli_formatters import ResultFormatter
from quarry.formatting import (
    format_collections,
    format_document_detail,
    format_documents,
)

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class DocumentsCli:
    """Serve ``show``/``delete``/``list`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``show``, ``delete``, and the ``list`` sub-app to *app*."""
        app.command(name="show")(self._p.cli_errors(self._show))
        app.command(name="delete")(self._p.cli_errors(self._delete))
        app.add_typer(self._build_list(), name="list")

    def _show(
        self,
        document_name: Annotated[str, typer.Argument(help="Document name")],
        page: Annotated[
            int | None, typer.Option("--page", "-p", help="Page number to display")
        ] = None,
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Scope to collection")
        ] = "",
    ) -> None:
        """Show document metadata or a specific page's text."""
        if page is not None and page < 1:
            self._p.err_console.print(
                f"Error: page number must be >= 1, got {page}", style="red"
            )
            raise typer.Exit(code=1)
        req = ShowRequest(document=document_name, collection=collection, page=page)
        client = self._p.client()
        if page is not None:
            resp = client.show_page(req)
            self._p.emit(
                resp.model_dump(),
                f"Document: {resp.document_name}\nPage: {resp.page_number}\n---\n"
                f"{resp.text}",
            )
            return
        info = client.show_document(req)
        detail = info.model_dump()
        self._p.emit(detail, format_document_detail(detail))

    def _delete(
        self,
        name: Annotated[str, typer.Argument(help="Document or collection to delete")],
        kind: Annotated[
            str,
            typer.Option("--type", "-t", help="What to delete: document or collection"),
        ] = "document",
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Scope (documents only)")
        ] = "",
    ) -> None:
        """Delete indexed data for a document or collection (dispatch only).

        Returns the daemon's task id; the chunk removal runs as a background
        task (fire-and-forget, DES-001).  An unknown type is rejected here.
        """
        client = self._p.client()
        if kind == "document":
            accepted = client.delete_document(
                DeleteDocumentRequest(name=name, collection=collection)
            )
        elif kind == "collection":
            accepted = client.delete_collection(DeleteCollectionRequest(name=name))
        else:
            self._p.err_console.print(
                f"Error: unknown type {kind!r}. Use 'document' or 'collection'.",
                style="red",
            )
            raise typer.Exit(code=1)
        self._p.emit(
            accepted.model_dump(),
            f"Delete {accepted.status}: task_id={accepted.task_id}",
        )

    def _build_list(self) -> typer.Typer:
        """Build the ``list`` sub-app (documents/collections/databases/regs)."""
        app = typer.Typer(
            help="List documents, collections, databases, or registrations.",
            invoke_without_command=True,
            rich_markup_mode=None,
        )
        app.callback(invoke_without_command=True)(self._list_callback)
        app.command(name="documents")(self._p.cli_errors(self._list_documents))
        app.command(name="collections")(self._p.cli_errors(self._list_collections))
        app.command(name="registrations")(self._p.cli_errors(self._list_registrations))
        app.command(name="databases")(self._p.cli_errors(self._list_databases))
        return app

    def _list_callback(self, ctx: typer.Context) -> None:
        """List documents, collections, databases, or registrations."""
        if ctx.invoked_subcommand is None:
            self._p.err_console.print(
                "Error: specify a noun — documents, collections, "
                "databases, or registrations.",
                style="red",
            )
            raise typer.Exit(code=1)

    def _list_documents(
        self,
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Filter by collection")
        ] = "",
    ) -> None:
        """List all indexed documents."""
        resp = self._p.client().list_documents(collection)
        docs = [doc.model_dump() for doc in resp.documents]
        self._p.emit(docs, format_documents(docs))

    def _list_collections(self) -> None:
        """List all collections with document and chunk counts."""
        resp = self._p.client().list_collections()
        cols = [col.model_dump() for col in resp.collections]
        self._p.emit(cols, format_collections(cols))

    def _list_registrations(self) -> None:
        """List all registered directories."""
        resp = self._p.client().list_registrations()
        regs = [reg.model_dump() for reg in resp.registrations]
        self._p.emit(regs, ResultFormatter.registrations(regs))

    def _list_databases(self) -> None:
        """List the single database the daemon is fixed to."""
        resp = self._p.client().list_databases()
        dbs = [db.model_dump() for db in resp.databases]
        self._p.emit(dbs, ResultFormatter.databases(dbs))
