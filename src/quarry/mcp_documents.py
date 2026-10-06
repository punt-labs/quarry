"""Document/collection lifecycle MCP tools — a sibling of ``McpTools``.

``show``, ``delete``, ``register_directory``, ``deregister_directory``, and
``sync_all_registrations`` all manage the identity of what's tracked on disk
(a document, a collection, a registered directory), distinct from ``McpTools``'
retrieval/write tools (``find``, ``ingest``, ``remember``, ``learn``) and from
``ResourceCatalog``'s read-only listing. Holds only the client factory,
matching every sibling tool module: no engine, no database, a fresh
connection resolved per call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.api import (
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    DeregisterRequest,
    RegisterRequest,
    ShowRequest,
)
from quarry.client import HttpError, TargetResolver
from quarry.formatting import format_document_detail
from quarry.mcp_guard import ToolGuard

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.mcpserver import MCPServer

    from quarry.client import QuarryClient

# The daemon returns 404 for a missing document/page; `show` translates it into a
# plain "not found" line rather than the guard's terse "Error: HttpError: …".
_NOT_FOUND = 404


@final
class DocumentTools:
    """The document/collection/registration lifecycle tool surface.

    Every method here manages what's tracked — a document's presence, a
    registered directory, a collection's existence — as opposed to searching
    or writing content into it.
    """

    __slots__ = ("_connect",)

    _connect: Callable[[], QuarryClient]

    def __new__(
        cls, connect: Callable[[], QuarryClient] = TargetResolver.connect
    ) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        return self

    def register(self, server: MCPServer) -> None:
        """Attach every guarded tool to *server* under its method name."""
        server.add_tool(self.show)
        server.add_tool(self.delete)
        server.add_tool(self.register_directory)
        server.add_tool(self.deregister_directory)
        server.add_tool(self.sync_all_registrations)

    @ToolGuard.wrap
    def show(
        self,
        document_name: str,
        page_number: int = 0,
        collection: str = "",
    ) -> str:
        (
            """Use to read a specific page, or to check whether a document is """
            """already indexed.

        Without page_number: shows document metadata (pages, chunks, collection).
        With page_number: shows the full text for that page.

        Args:
            document_name: Document filename (e.g., 'report.pdf').
            page_number: Page number (1-indexed). 0 means show metadata only.
            collection: Optional collection scope.
        """
        )
        if err := self._reject_blank(document_name, "document_name"):
            return err
        client = self._connect()
        # A page is 1-based: 0 or negative means "no page" (metadata), never a
        # nonsensical page sent to the daemon.
        req = ShowRequest(
            document=document_name,
            collection=collection,
            page=page_number if page_number > 0 else None,
        )
        try:
            if page_number > 0:
                page = client.show_page(req)
                return (
                    f"Document: {page.document_name}\nPage: {page.page_number}\n---\n"
                    f"{page.text}"
                )
            return format_document_detail(client.show_document(req).model_dump())
        except HttpError as exc:
            # A 404 is the documented "no such document/page" outcome — render the
            # plain domain message a model expects, not the guard's "Error: HttpError".
            if exc.status != _NOT_FOUND:
                raise
            if page_number > 0:
                return f"No data found for {document_name} page {page_number}"
            return f"Document {document_name!r} not found"

    @ToolGuard.wrap
    def delete(
        self,
        name: str,
        kind: str = "document",
        collection: str = "",
    ) -> str:
        """Use to remove stale or wrong content before re-ingesting it.

        Returns immediately — the daemon removes chunks in the background.

        Args:
            name: Document filename or collection name to delete.
            kind: What to delete — "document" or "collection".
            collection: Optional collection scope (only for kind="document").
        """
        # Validate the input before reaching for the daemon: a blank name or bad
        # kind is a caller error, answerable without a connection.
        if err := self._reject_blank(name, "name"):
            return err
        if kind not in ("document", "collection"):
            return f"Error: Invalid kind {kind!r}. Must be 'document' or 'collection'."
        client = self._connect()
        if kind == "document":
            accepted = client.delete_document(
                DeleteDocumentRequest(name=name, collection=collection)
            )
        else:
            accepted = client.delete_collection(DeleteCollectionRequest(name=name))
        return f"▶  Deleting {kind} {name!r} (task {accepted.task_id})"

    @ToolGuard.wrap
    def register_directory(self, directory: str, collection: str = "") -> str:
        """Use to track a local directory so future changes sync automatically.

        Returns immediately — the daemon records the registration in the background.

        Args:
            directory: Absolute path to the directory.
            collection: Collection name. Uses directory name if empty.
        """
        from pathlib import Path  # noqa: PLC0415 — path leaf only, no engine

        # An empty directory would resolve to the cwd and silently register it.
        if err := self._reject_blank(directory, "directory"):
            return err
        resolved = Path(directory).expanduser().resolve()
        col = collection or resolved.name or "root"
        accepted = self._connect().register(
            RegisterRequest(directory=str(resolved), collection=col)
        )
        return f"▶  Registering {resolved} as {col!r} (task {accepted.task_id})"

    @ToolGuard.wrap
    def deregister_directory(self, collection: str, keep_data: bool = False) -> str:
        (
            """Use to stop tracking a directory — keep its indexed data with """
            """``keep_data=True``, or purge it.

        Returns the removed-file count synchronously; the chunk purge runs as a
        background task. An unknown collection surfaces as an error, not a
        removal confirmation.

        Args:
            collection: Collection name to deregister.
            keep_data: If true, keep indexed data in LanceDB.
        """
        )
        if err := self._reject_blank(collection, "collection"):
            return err
        accepted = self._connect().deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        )
        return (
            f"Deregistered collection {collection!r} ({accepted.removed} files); "
            f"chunk purge task {accepted.task_id}"
        )

    @ToolGuard.wrap
    def sync_all_registrations(self) -> str:
        (
            """Use after registering a new directory, or when tracked files """
            """changed outside quarry's own writes.

        Returns immediately — the daemon runs the sync in the background.
        """
        )
        accepted = self._connect().sync()
        return f"▶  Syncing all registrations (task {accepted.task_id})"

    @staticmethod
    def _reject_blank(value: str, label: str) -> str | None:
        # Returns an Error: string for a blank required arg, else None. None is
        # the documented "valid" signal (no error) — the caller proceeds on None,
        # so this is an appropriate Optional, not a give-up value.
        if value.strip():
            return None
        return f"Error: {label} is required (got an empty value)."
