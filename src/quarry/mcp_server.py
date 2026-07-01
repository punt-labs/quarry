from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from quarry.collections import CollectionName
from quarry.config import Settings
from quarry.db import Database
from quarry.db.storage import dir_size_bytes, discover_databases
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
from quarry.ingestion.backends import get_embedding_backend
from quarry.ingestion.pipeline import (
    ingest_auto as pipeline_ingest_auto,
    ingest_content as pipeline_ingest_content,
    ingest_document,
)
from quarry.ingestion.provider import ProviderSelection
from quarry.logging_config import LoggingConfig
from quarry.sync import sync_all as engine_sync_all
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from anyio.streams.memory import (
        MemoryObjectReceiveStream,
        MemoryObjectSendStream,
    )
    from mcp.shared.message import SessionMessage

LoggingConfig.configure(stderr_level="INFO")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "punt-quarry",
    instructions=(
        "Quarry is a local semantic search engine for your knowledge base. "
        "Use these tools to ingest, search, and manage documents.\n\n"
        "All quarry tool output is pre-formatted plain text using unicode "
        "characters for alignment. Always emit quarry output verbatim \u2014 "
        "never reformat, never convert to markdown tables, never wrap "
        "in code fences or boxes."
    ),
)

_db_name: ContextVar[str | None] = ContextVar("db_name", default=None)


def _handle_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """Catch exceptions at the MCP boundary, log, and return error string."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.exception("Error in %s", fn.__name__)
            return f"Error: {type(exc).__name__}: {exc}"

    return wrapper


def _settings() -> Settings:
    return Settings.load().resolve_db_paths(_db_name.get())


def _database() -> Database:
    return Database.connect(_settings().lancedb_path)


_executor = ThreadPoolExecutor(max_workers=4)


def _background(fn: Callable[..., object], *args: object, **kwargs: object) -> None:
    """Run *fn* in the bounded thread pool.  Exceptions are logged, not raised."""

    def _target() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Background task %s failed", fn.__name__)

    _executor.submit(_target)


@mcp.tool()
@_handle_errors
def find(
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
    limit = min(limit, 50)
    settings = _settings()
    database = _database()

    query_vector = get_embedding_backend(settings).embed_query(query)

    results = database.search.hybrid_search(
        query,
        query_vector,
        limit=limit,
        document_filter=document_filter or None,
        collection_filter=collection or None,
        page_type_filter=page_type or None,
        source_format_filter=source_format or None,
        agent_handle_filter=agent_handle or None,
        memory_type_filter=memory_type or None,
    )

    formatted = [
        {
            "document_name": r["document_name"],
            "collection": r["collection"],
            "page_number": r["page_number"],
            "chunk_index": r["chunk_index"],
            "text": r["text"],
            "page_type": r["page_type"],
            "source_format": r["source_format"],
            "similarity": round(1 - float(str(r.get("_distance", 0))), 4),
        }
        for r in results
    ]

    return format_search_results(query, formatted)


def _do_ingest(
    source: str,
    overwrite: bool,
    collection: str,
    settings: Settings,
    database: Database,
) -> None:
    """Blocking ingest — runs in background thread."""
    if source.startswith(("http://", "https://")):
        pipeline_ingest_auto(
            source,
            database,
            settings,
            overwrite=overwrite,
            collection=collection or "",
        )
        return

    path = Path(source)
    col = CollectionName.from_path(path, explicit=collection or None)
    ingest_document(path, database, settings, overwrite=overwrite, collection=str(col))


@mcp.tool()
@_handle_errors
def ingest(
    source: str,
    overwrite: bool = False,
    collection: str = "",
) -> str:
    """Ingest a file or URL into the knowledge base.

    Auto-detects the source type: URLs (http/https) use smart sitemap
    discovery with single-page fallback; local paths are ingested as files.

    Supported formats: PDF, images (PNG, JPG, TIFF, BMP, WebP), presentations
    (PPTX), spreadsheets (XLSX, CSV), HTML, TXT, MD, TEX, DOCX, and source
    code files.

    Returns immediately — ingestion runs in the background.

    Args:
        source: File path or HTTP(S) URL to ingest.
        overwrite: If true, replace existing data.
        collection: Collection name. Auto-derived if empty.
    """
    settings = _settings()
    database = _database()
    _background(_do_ingest, source, overwrite, collection, settings, database)
    return f"\u25b6  Ingesting {source} (background)"


def _do_remember(
    content: str,
    document_name: str,
    overwrite: bool,
    collection: str,
    format_hint: str,
    agent_handle: str,
    memory_type: str,
    summary: str,
    settings: Settings,
    database: Database,
) -> None:
    """Blocking remember — runs in background thread."""
    pipeline_ingest_content(
        content,
        document_name,
        database,
        settings,
        overwrite=overwrite,
        collection=collection,
        format_hint=format_hint,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )


@mcp.tool()
@_handle_errors
def remember(
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

    Returns immediately — indexing runs in the background.

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
    settings = _settings()
    database = _database()
    _background(
        _do_remember,
        content,
        document_name,
        overwrite,
        collection,
        format_hint,
        agent_handle,
        memory_type,
        summary,
        settings,
        database,
    )
    return f"\u25b6  Remembering {document_name} (background)"


@mcp.tool(name="list")
@_handle_errors
def list_resources(
    kind: str,
    collection: str = "",
) -> str:
    """List documents, collections, databases, or registrations.

    Args:
        kind: What to list — "documents", "collections", "databases",
              or "registrations".
        collection: Optional collection filter (only for kind="documents").
    """
    if kind == "documents":
        database = _database()
        docs = database.catalog.list_documents(collection_filter=collection or None)
        return format_documents(docs)
    if kind == "collections":
        database = _database()
        cols = database.catalog.list_collections()
        return format_collections(cols)
    if kind == "databases":
        settings = _settings()
        databases = discover_databases(settings.quarry_root)
        return format_databases(databases, current=_db_name.get() or "default")
    if kind == "registrations":
        settings = _settings()
        conn = SyncRegistry(settings.registry_path)
        try:
            regs = conn.list_registrations()
        finally:
            conn.close()
        return format_registrations(
            [
                {
                    "directory": r.directory,
                    "collection": r.collection,
                    "registered_at": r.registered_at,
                }
                for r in regs
            ]
        )
    return (
        f"Error: unknown kind {kind!r}. "
        "Use documents, collections, databases, or registrations."
    )


@mcp.tool()
@_handle_errors
def show(
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
    database = _database()

    if page_number > 0:
        text = database.catalog.get_page_text(
            document_name, page_number, collection=collection or None
        )
        if text is None:
            return f"No data found for {document_name} page {page_number}"
        return f"Document: {document_name}\nPage: {page_number}\n---\n{text}"

    docs = database.catalog.list_documents(collection_filter=collection or None)
    match = [d for d in docs if d["document_name"] == document_name]
    if not match:
        return f"Document {document_name!r} not found"
    return format_document_detail(match[0])


def _do_delete(name: str, kind: str, collection: str, database: Database) -> None:
    """Blocking delete — runs in background thread."""
    store = database.store
    if kind == "collection":
        store.delete_collection(name)
    else:
        store.delete_document(name, collection=collection or None, count=False)


@mcp.tool()
@_handle_errors
def delete(
    name: str,
    kind: str = "document",
    collection: str = "",
) -> str:
    """Delete indexed data for a document or collection.

    Returns immediately — deletion runs in the background.

    Args:
        name: Document filename or collection name to delete.
        kind: What to delete — "document" or "collection".
        collection: Optional collection scope (only for kind="document").
    """
    if kind not in ("document", "collection"):
        return f"Invalid kind {kind!r}. Must be 'document' or 'collection'."
    database = _database()
    _background(_do_delete, name, kind, collection, database)
    return f"\u25b6  Deleting {kind} {name!r} (background)"


def _do_register(directory: str, collection: str, settings: Settings) -> None:
    """Blocking register — runs in background thread."""
    path = Path(directory).resolve()
    col = collection or path.name
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(path, col)
    finally:
        conn.close()


@mcp.tool()
@_handle_errors
def register_directory(directory: str, collection: str = "") -> str:
    """Register a directory for incremental sync.

    Returns immediately — registration runs in the background.

    Args:
        directory: Absolute path to the directory.
        collection: Collection name. Uses directory name if empty.
    """
    settings = _settings()
    path = Path(directory).resolve()
    col = collection or path.name
    _background(_do_register, directory, collection, settings)
    return f"\u25b6  Registering {path} as {col!r} (background)"


def _do_deregister(
    collection: str, keep_data: bool, settings: Settings, database: Database
) -> str:
    """Deregister synchronously: existence check, registry delete, chunk purge."""
    conn = SyncRegistry(settings.registry_path)
    try:
        if conn.get_registration(collection) is None:
            return f"No registration found for {collection!r}"
        doc_names = conn.deregister_directory(collection)
    finally:
        conn.close()
    purge = [] if keep_data else doc_names
    deleted_chunks = sum(
        database.store.delete_document(name, collection=collection) for name in purge
    )
    return (
        f"Deregistered collection {collection!r} "
        f"({len(doc_names)} files, {deleted_chunks} chunks deleted)"
    )


@mcp.tool()
@_handle_errors
def deregister_directory(collection: str, keep_data: bool = False) -> str:
    """Remove a directory registration synchronously.

    Unknown collection returns 'No registration found'; a failure is reported.

    Args:
        collection: Collection name to deregister.
        keep_data: If true, keep indexed data in LanceDB.
    """
    return _do_deregister(collection, keep_data, _settings(), _database())


def _do_sync(settings: Settings, database: Database) -> None:
    """Blocking sync — runs in background thread."""
    engine_sync_all(database.db, settings)


@mcp.tool()
@_handle_errors
def sync_all_registrations() -> str:
    """Sync all registered directories: ingest new/changed, remove deleted.

    Returns immediately — sync runs in the background.
    """
    settings = _settings()
    database = _database()
    _background(_do_sync, settings, database)
    return "\u25b6  Syncing all registrations (background)"


@mcp.tool()
@_handle_errors
def status() -> str:
    """Get database status: document/chunk counts, storage size, and model info."""
    settings = _settings()
    database = _database()

    docs = database.catalog.list_documents()
    chunks = database.store.count()
    cols = database.catalog.list_collections()

    if settings.registry_path.exists():
        conn = SyncRegistry(settings.registry_path)
        try:
            regs = conn.list_registrations()
        finally:
            conn.close()
    else:
        regs = []

    db_size_bytes = (
        dir_size_bytes(settings.lancedb_path) if settings.lancedb_path.exists() else 0
    )

    return format_status(
        {
            "document_count": len(docs),
            "collection_count": len(cols),
            "chunk_count": chunks,
            "registered_directories": len(regs),
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
            "provider": ProviderSelection.display_cached(),
        }
    )


@mcp.tool(name="use")
@_handle_errors
def use_database(name: str) -> str:
    """Switch to a different named database for subsequent operations.

    All tools (find, ingest, sync, etc.) will use the selected database
    until changed again. Use list(kind="databases") to see available databases.

    Args:
        name: Database name (e.g., 'coding', 'work'). Use 'default' for
              the default database.
    """
    previous = _db_name.get() or "default"
    new_name = name if name != "default" else None
    # Validate before mutating: resolve_db_paths raises ValueError for
    # names containing path separators or traversal segments.
    test_settings = Settings.load().resolve_db_paths(new_name)
    summary = format_switch_summary(previous, name, str(test_settings.lancedb_path))
    _db_name.set(new_name)
    return summary


async def run_mcp_session(
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
) -> None:
    """Run an MCP session on the given streams (WebSocket, stdio, etc.).

    This is the public entry point for non-stdio transports.  Each call
    gets its own ``ServerSession`` with isolated ContextVar state.
    """
    server = getattr(mcp, "_mcp_server", None)
    if server is None:
        msg = (
            f"FastMCP._mcp_server not found (mcp=={__import__('mcp').__version__}). "
            "This private API may have changed; punt-quarry requires mcp<2.0.0."
        )
        raise RuntimeError(msg)
    await server.run(
        read_stream,
        write_stream,
        server.create_initialization_options(),
    )


def main(db_name: str | None = None) -> None:
    _db_name.set(db_name)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
