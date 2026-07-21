"""The ``quarry mcp`` stdio server — a FastMCP client of ``quarryd`` (DES-031 v2.2).

Every tool body is a thin :class:`~quarry.client.QuarryClient` call over the
daemon's ``/v1`` REST API; this module imports **no engine** (no ``quarry.db``,
``embeddings``, ``ingestion``, or ``retrieval``), so ``import quarry.mcp_server``
and running ``quarry mcp`` load zero LanceDB/ONNX.  It mirrors vox's
``vox mcp`` → ``server.py`` → ``VoxClientSync`` shape: the MCP server is a client
of the resident daemon, never a second in-process engine.

The eleven tools and their docstrings are the surface Claude Code sees; the
bodies changed (client calls, fire-and-forget 202s), the surface did not.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Self, final

from mcp.server.fastmcp import FastMCP

from quarry.api import (
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    DeregisterRequest,
    IngestRequest,
    RegisterRequest,
    RememberRequest,
    SearchRequest,
    ShowRequest,
)
from quarry.client import HttpError, QuarryClient, TargetResolver
from quarry.config import Settings
from quarry.formatting import (
    format_collections,
    format_databases,
    format_document_detail,
    format_documents,
    format_registrations,
    format_search_results,
    format_status,
    format_switch_summary,
)
from quarry.logging_config import LoggingConfig

logger = logging.getLogger(__name__)

# The daemon returns 404 for a missing document/page; `show` translates it into a
# plain "not found" line rather than the guard's terse "Error: HttpError: …".
_NOT_FOUND = 404


def _guard(method: Callable[..., str]) -> Callable[..., str]:
    """Wrap a tool method so any failure returns an error string at the boundary.

    A down daemon (``QuarryConnectionError``), a daemon rejection (``HttpError`` —
    e.g. a 404 on an unknown collection), or any escape is logged and rendered as
    ``Error: …`` rather than propagating; the stdio transport never sees a raised
    exception, and there is no in-process engine fallback.  Applied at definition
    so a direct call and the registered tool share the identical boundary.
    """

    @functools.wraps(method)
    def wrapper(*args: object, **kwargs: object) -> str:
        try:
            return method(*args, **kwargs)
        # The MCP tool-handler boundary: any failure becomes a returned string.
        except Exception as exc:
            logger.exception("Error in %s", method.__name__)
            return f"Error: {type(exc).__name__}: {exc}"

    return wrapper


mcp = FastMCP(
    "punt-quarry",
    instructions=(
        "Quarry is a local semantic search engine for your knowledge base. "
        "Use these tools to ingest, search, and manage documents.\n\n"
        "All quarry tool output is pre-formatted plain text using unicode "
        "characters for alignment. Always emit quarry output verbatim — "
        "never reformat, never convert to markdown tables, never wrap "
        "in code fences or boxes."
    ),
)


@final
class McpTools:
    """The MCP tool surface, each tool a thin :class:`QuarryClient` call.

    Holds only the client factory it resolves per call (fresh connection per
    tool, matching vox) — no engine, no thread pool, no database.  A down daemon
    surfaces as a clean MCP error string via :meth:`_guard`, never an in-process
    engine fallback.  Tests inject a factory returning a client over an
    ``ASGITransport`` so each tool round-trips through the real daemon handlers.
    """

    __slots__ = ("_connect",)

    _connect: Callable[[], QuarryClient]

    def __new__(
        cls, connect: Callable[[], QuarryClient] = TargetResolver.connect
    ) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        return self

    def register(self, server: FastMCP) -> None:
        """Attach every guarded tool to *server* under its wire name.

        ``list`` and ``use`` keep their short wire names; the rest register under
        the method name.  Every method is already ``@_guard``-wrapped, so the
        registered tool and a direct call share the identical error boundary.
        """
        server.add_tool(self.find)
        server.add_tool(self.ingest)
        server.add_tool(self.remember)
        server.add_tool(self.list_resources, name="list")
        server.add_tool(self.show)
        server.add_tool(self.delete)
        server.add_tool(self.register_directory)
        server.add_tool(self.deregister_directory)
        server.add_tool(self.sync_all_registrations)
        server.add_tool(self.status)
        server.add_tool(self.use_database, name="use")

    @_guard
    def find(
        self,
        query: str,
        limit: int = 10,
        document_filter: str = "",
        collection: str = "",
        page_type: str = "",
        source_format: str = "",
        agent_handle: str = "",
        memory_type: str = "",
    ) -> str:
        """Search indexed documents using hybrid semantic + keyword search.

        Combines vector similarity and BM25 full-text search via Reciprocal
        Rank Fusion (RRF) for better recall on both meaning and exact terms.

        Args:
            query: Natural language search query.
            limit: Maximum number of results (default 10, max 50).
            document_filter: Optional exact document name to filter by.
            collection: Optional collection name to search within.
            page_type: Optional content type filter (text, code, spreadsheet, etc.).
            source_format: Optional source format filter (.pdf, .py, .xlsx, etc.).
            agent_handle: Optional agent handle to filter by (e.g. "rmh").
            memory_type: Optional memory type filter (fact, observation, etc.).
        """
        req = SearchRequest(
            query=query,
            limit=min(limit, 50),
            collection=collection,
            document=document_filter,
            page_type=page_type,
            source_format=source_format,
            agent_handle=agent_handle,
            memory_type=memory_type,
        )
        resp = self._connect().search(req)
        return format_search_results(query, [hit.model_dump() for hit in resp.results])

    @_guard
    def ingest(
        self,
        source: str,
        overwrite: bool = False,
        collection: str = "",
    ) -> str:
        """Ingest an HTTP(S) URL into the knowledge base.

        Fetches a URL with smart sitemap discovery and single-page fallback.
        For local files and directories, use ``register`` + ``sync`` — the
        daemon owns the filesystem, so there is no in-process file loader here.

        Returns immediately — the daemon indexes in the background.

        Args:
            source: HTTP(S) URL to ingest.
            overwrite: If true, replace existing data.
            collection: Collection name. Auto-derived if empty.
        """
        if not source.startswith(("http://", "https://")):
            return (
                f"Error: {source!r} is not a URL. Use register(directory=...) to "
                "track local files and directories, then sync_all_registrations()."
            )
        accepted = self._connect().ingest_url(
            IngestRequest(source=source, overwrite=overwrite, collection=collection)
        )
        return f"▶  Ingesting {source} (task {accepted.task_id})"

    @_guard
    def remember(
        self,
        content: str,
        document_name: str,
        overwrite: bool = False,
        collection: str = "default",
        format_hint: str = "auto",
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> str:
        """Remember inline text content: chunk, embed, and index for search.

        Use this instead of ingest when you have the text content directly
        (e.g., clipboard, API response, or sandbox-uploaded files in Claude Desktop).
        The daemon scrubs secrets/PII before indexing.

        Returns immediately — the daemon indexes in the background.

        Args:
            content: The text content to remember.
            document_name: Name for the document (e.g., 'notes.md').
            overwrite: If true, replace existing data for this document.
            collection: Collection name (default: 'default').
            format_hint: Format hint: 'auto', 'plain', 'markdown', 'latex'.
            agent_handle: Agent that owns this memory (e.g. "rmh").
            memory_type: Memory classification: fact, observation, opinion, procedure.
            summary: One-line summary of the content.
        """
        # The MCP surface deliberately defaults overwrite=False (unlike the CLI and
        # the RememberRequest model default of True): an agent calling remember
        # should add, not silently replace. The value is always passed explicitly.
        accepted = self._connect().remember(
            RememberRequest(
                name=document_name,
                content=content,
                overwrite=overwrite,
                collection=collection,
                format_hint=format_hint,
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )
        )
        return f"▶  Remembering {document_name} (task {accepted.task_id})"

    @_guard
    def list_resources(self, kind: str, collection: str = "") -> str:
        """List documents, collections, databases, or registrations.

        Args:
            kind: What to list — "documents", "collections", "databases",
                  or "registrations".
            collection: Optional collection filter (only for kind="documents").
        """
        handler = {
            "documents": self._list_documents,
            "collections": self._list_collections,
            "databases": self._list_databases,
            "registrations": self._list_registrations,
        }.get(kind)
        if handler is None:
            return (
                f"Error: unknown kind {kind!r}. "
                "Use documents, collections, databases, or registrations."
            )
        return handler(collection)

    @_guard
    def show(
        self,
        document_name: str,
        page_number: int = 0,
        collection: str = "",
    ) -> str:
        """Show document metadata or retrieve a specific page's text.

        Without page_number: shows document metadata (pages, chunks, collection).
        With page_number: shows the full text for that page.

        Args:
            document_name: Document filename (e.g., 'report.pdf').
            page_number: Page number (1-indexed). 0 means show metadata only.
            collection: Optional collection scope.
        """
        client = self._connect()
        req = ShowRequest(
            document=document_name, collection=collection, page=page_number or None
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

    @_guard
    def delete(
        self,
        name: str,
        kind: str = "document",
        collection: str = "",
    ) -> str:
        """Delete indexed data for a document or collection.

        Returns immediately — the daemon removes chunks in the background.

        Args:
            name: Document filename or collection name to delete.
            kind: What to delete — "document" or "collection".
            collection: Optional collection scope (only for kind="document").
        """
        # Validate the input before reaching for the daemon: a bad kind is a
        # caller error, answerable without a connection.
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

    @_guard
    def register_directory(self, directory: str, collection: str = "") -> str:
        """Register a directory for incremental sync.

        Returns immediately — the daemon records the registration in the background.

        Args:
            directory: Absolute path to the directory.
            collection: Collection name. Uses directory name if empty.
        """
        from pathlib import Path  # noqa: PLC0415 — path leaf only, no engine

        resolved = Path(directory).expanduser().resolve()
        col = collection or resolved.name or "root"
        accepted = self._connect().register(
            RegisterRequest(directory=str(resolved), collection=col)
        )
        return f"▶  Registering {resolved} as {col!r} (task {accepted.task_id})"

    @_guard
    def deregister_directory(self, collection: str, keep_data: bool = False) -> str:
        """Remove a directory registration.

        Returns the removed-file count synchronously; the chunk purge runs as a
        background task. An unknown collection surfaces as an error, not a
        removal confirmation.

        Args:
            collection: Collection name to deregister.
            keep_data: If true, keep indexed data in LanceDB.
        """
        accepted = self._connect().deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        )
        return (
            f"Deregistered collection {collection!r} ({accepted.removed} files); "
            f"chunk purge task {accepted.task_id}"
        )

    @_guard
    def sync_all_registrations(self) -> str:
        """Sync all registered directories: ingest new/changed, remove deleted.

        Returns immediately — the daemon runs the sync in the background.
        """
        accepted = self._connect().sync()
        return f"▶  Syncing all registrations (task {accepted.task_id})"

    @_guard
    def status(self) -> str:
        """Get database status: document/chunk counts, storage size, and model info."""
        return format_status(self._connect().status().model_dump())

    @_guard
    def use_database(self, name: str) -> str:
        """Switch to a different named database for subsequent operations.

        All tools (find, ingest, sync, etc.) will target the selected database's
        daemon until changed again. Use list(kind="databases") to see the
        database the daemon is fixed to.

        Args:
            name: Database name (e.g., 'coding', 'work'). Use 'default' for
                  the default database.
        """
        previous = Settings.active_db() or "default"
        # Select the literal named db, "default" included — never fall through to
        # the persistent read_default_db(), or use("default") would silently pick
        # whatever the CLI last persisted and the summary path would lie about the
        # target subsequent tools connect to. Validate before mutating:
        # resolve_db_paths raises ValueError on a bad name, leaving the db unchanged.
        resolved = Settings.load().resolve_db_paths(name)
        Settings.set_active_db(name)
        return format_switch_summary(previous, name, str(resolved.lancedb_path))

    def _list_documents(self, collection: str) -> str:
        docs = self._connect().list_documents(collection)
        return format_documents([doc.model_dump() for doc in docs.documents])

    def _list_collections(self, _collection: str) -> str:
        cols = self._connect().list_collections()
        return format_collections([col.model_dump() for col in cols.collections])

    def _list_databases(self, _collection: str) -> str:
        dbs = self._connect().list_databases()
        current = Settings.active_db() or "default"
        return format_databases(
            [db.model_dump() for db in dbs.databases], current=current
        )

    def _list_registrations(self, _collection: str) -> str:
        regs = self._connect().list_registrations()
        return format_registrations([reg.model_dump() for reg in regs.registrations])


_tools = McpTools()
_tools.register(mcp)


def main(db_name: str | None = None) -> None:
    """Run the stdio MCP server, targeting *db_name* (the daemon's database)."""
    LoggingConfig.configure(stderr_level="INFO")
    Settings.set_active_db(db_name or "")
    logger.info("Starting quarry MCP server (client tier)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
