from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from quarry.backends import get_embedding_backend
from quarry.collections import derive_collection
from quarry.config import Settings, configure_logging, load_settings, resolve_db_paths
from quarry.database import (
    count_chunks,
    delete_collection as db_delete_collection,
    delete_document as db_delete_document,
    discover_databases,
    get_db,
    get_page_text,
    list_collections as db_list_collections,
    list_documents,
    search,
)
from quarry.pipeline import (
    ingest_content as pipeline_ingest_content,
    ingest_document,
    ingest_url as pipeline_ingest_url,
)
from quarry.sync import sync_all as engine_sync_all
from quarry.sync_registry import (
    deregister_directory as registry_deregister,
    list_registrations as registry_list,
    open_registry,
    register_directory as registry_register,
)
from quarry.types import LanceDB

configure_logging(load_settings())
logger = logging.getLogger(__name__)

mcp = FastMCP("quarry-mcp")

_db_name: str | None = None


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
    return resolve_db_paths(load_settings(), _db_name)


def _db() -> LanceDB:
    return get_db(_settings().lancedb_path)


@mcp.tool()
@_handle_errors
def search_documents(
    query: str,
    limit: int = 10,
    document_filter: str = "",
    collection: str = "",
    page_type: str = "",
    source_format: str = "",
) -> str:
    """Search indexed documents using semantic similarity.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 10, max 50).
        document_filter: Optional exact document name to filter by.
        collection: Optional collection name to search within.
        page_type: Optional content type filter (text, code, spreadsheet, etc.).
        source_format: Optional source format filter (.pdf, .py, .xlsx, etc.).
    """
    limit = min(limit, 50)
    settings = _settings()
    db = _db()

    query_vector = get_embedding_backend(settings).embed_query(query)

    results = search(
        db,
        query_vector,
        limit=limit,
        document_filter=document_filter or None,
        collection_filter=collection or None,
        page_type_filter=page_type or None,
        source_format_filter=source_format or None,
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

    return json.dumps(
        {
            "query": query,
            "total_results": len(formatted),
            "results": formatted,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def ingest_file(
    file_path: str,
    overwrite: bool = False,
    collection: str = "",
) -> str:
    """Ingest a document from a file path: OCR, chunk, embed, and index for search.

    Supported formats: PDF, images (PNG, JPG, TIFF, BMP, WebP), presentations (PPTX),
    spreadsheets (XLSX, CSV), HTML, TXT, MD, TEX, DOCX, and source code files.

    Args:
        file_path: Path to the document file.
        overwrite: If true, replace existing data for this document.
        collection: Collection name. Auto-derived from parent directory if empty.
    """
    path = Path(file_path)
    settings = _settings()
    db = _db()
    col = derive_collection(path, explicit=collection or None)

    progress_lines: list[str] = []

    result = ingest_document(
        path,
        db,
        settings,
        overwrite=overwrite,
        collection=col,
        progress_callback=progress_lines.append,
    )

    progress_lines.append("")
    progress_lines.append(f"Result: {json.dumps(result, indent=2)}")
    return "\n".join(progress_lines)


@mcp.tool()
@_handle_errors
def ingest_content(
    content: str,
    document_name: str,
    overwrite: bool = False,
    collection: str = "default",
    format_hint: str = "auto",
) -> str:
    """Ingest inline text content: chunk, embed, and index for search.

    Use this instead of ingest_file when you have the text content directly
    (e.g., clipboard, API response, or sandbox-uploaded files in Claude Desktop).

    Args:
        content: The text content to ingest.
        document_name: Name for the document (e.g., 'notes.md').
        overwrite: If true, replace existing data for this document.
        collection: Collection name (default: 'default').
        format_hint: Format hint: 'auto', 'plain', 'markdown', 'latex'.
    """
    settings = _settings()
    db = _db()

    progress_lines: list[str] = []

    result = pipeline_ingest_content(
        content,
        document_name,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        format_hint=format_hint,
        progress_callback=progress_lines.append,
    )

    progress_lines.append("")
    progress_lines.append(f"Result: {json.dumps(result, indent=2)}")
    return "\n".join(progress_lines)


@mcp.tool()
@_handle_errors
def ingest_url(
    url: str,
    overwrite: bool = False,
    collection: str = "default",
    document_name: str = "",
) -> str:
    """Fetch a webpage and ingest its content for search.

    Downloads the HTML, strips boilerplate (nav, scripts, etc.), converts to
    Markdown, and indexes the text. Use this for documentation pages, blog
    posts, or any web content you want to search semantically.

    Args:
        url: HTTP(S) URL to fetch.
        overwrite: If true, replace existing data for this URL.
        collection: Collection name (default: 'default').
        document_name: Override for stored name. Defaults to the URL.
    """
    settings = _settings()
    db = _db()

    progress_lines: list[str] = []

    result = pipeline_ingest_url(
        url,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        document_name=document_name or None,
        progress_callback=progress_lines.append,
    )

    progress_lines.append("")
    progress_lines.append(f"Result: {json.dumps(result, indent=2)}")
    return "\n".join(progress_lines)


@mcp.tool()
@_handle_errors
def get_documents(collection: str = "") -> str:
    """List all indexed documents with metadata.

    Args:
        collection: Optional collection name to filter by.
    """
    db = _db()
    docs = list_documents(db, collection_filter=collection or None)
    return json.dumps({"total_documents": len(docs), "documents": docs}, indent=2)


@mcp.tool()
@_handle_errors
def get_page(
    document_name: str,
    page_number: int,
    collection: str = "",
) -> str:
    """Retrieve the full raw OCR text for a specific document page.

    Args:
        document_name: Document filename (e.g., 'report.pdf').
        page_number: Page number (1-indexed).
        collection: Optional collection scope.
    """
    db = _db()
    text = get_page_text(db, document_name, page_number, collection=collection or None)

    if text is None:
        return f"No data found for {document_name} page {page_number}"

    return f"Document: {document_name}\nPage: {page_number}\n---\n{text}"


@mcp.tool()
@_handle_errors
def delete_document(
    document_name: str,
    collection: str = "",
) -> str:
    """Delete all indexed data for a document.

    Args:
        document_name: Document filename (e.g., 'report.pdf').
        collection: Optional collection scope. If empty, deletes across all collections.
    """
    db = _db()
    deleted = db_delete_document(db, document_name, collection=collection or None)
    return json.dumps(
        {
            "document_name": document_name,
            "collection": collection or "(all)",
            "chunks_deleted": deleted,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def list_collections() -> str:
    """List all collections with document and chunk counts."""
    db = _db()
    cols = db_list_collections(db)
    return json.dumps(
        {"total_collections": len(cols), "collections": cols},
        indent=2,
    )


@mcp.tool()
@_handle_errors
def delete_collection(collection: str) -> str:
    """Delete all indexed data for a collection.

    Args:
        collection: Collection name to delete.
    """
    db = _db()
    deleted = db_delete_collection(db, collection)
    return json.dumps(
        {
            "collection": collection,
            "chunks_deleted": deleted,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def register_directory(directory: str, collection: str = "") -> str:
    """Register a directory for incremental sync.

    Args:
        directory: Absolute path to the directory.
        collection: Collection name. Uses directory name if empty.
    """
    settings = _settings()
    path = Path(directory).resolve()
    col = collection or path.name
    conn = open_registry(settings.registry_path)
    try:
        reg = registry_register(conn, path, col)
    finally:
        conn.close()
    return json.dumps(
        {
            "directory": reg.directory,
            "collection": reg.collection,
            "registered_at": reg.registered_at,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def deregister_directory(
    collection: str,
    keep_data: bool = False,
) -> str:
    """Remove a directory registration.

    Args:
        collection: Collection name to deregister.
        keep_data: If true, keep indexed data in LanceDB.
    """
    settings = _settings()
    conn = open_registry(settings.registry_path)
    try:
        doc_names = registry_deregister(conn, collection)
    finally:
        conn.close()

    if not keep_data and doc_names:
        db = _db()
        for name in doc_names:
            db_delete_document(db, name, collection=collection)

    return json.dumps(
        {
            "collection": collection,
            "documents_removed": len(doc_names),
            "data_deleted": not keep_data,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def sync_all_registrations() -> str:
    """Sync all registered directories: ingest new/changed, remove deleted."""
    settings = _settings()
    db = _db()

    progress_lines: list[str] = []
    results = engine_sync_all(
        db,
        settings,
        progress_callback=progress_lines.append,
    )

    summary = {
        col: {
            "ingested": res.ingested,
            "deleted": res.deleted,
            "skipped": res.skipped,
            "failed": res.failed,
            "errors": res.errors,
        }
        for col, res in results.items()
    }

    return json.dumps(
        {
            "collections_synced": len(results),
            "results": summary,
            "progress": progress_lines,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def list_registrations() -> str:
    """List all registered directories."""
    settings = _settings()
    conn = open_registry(settings.registry_path)
    try:
        regs = registry_list(conn)
    finally:
        conn.close()
    return json.dumps(
        {
            "total_registrations": len(regs),
            "registrations": [
                {
                    "directory": r.directory,
                    "collection": r.collection,
                    "registered_at": r.registered_at,
                }
                for r in regs
            ],
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def status() -> str:
    """Get database status: document/chunk counts, storage size, and model info."""
    settings = _settings()
    db = _db()

    docs = list_documents(db)
    chunks = count_chunks(db)
    cols = db_list_collections(db)

    if settings.registry_path.exists():
        conn = open_registry(settings.registry_path)
        try:
            regs = registry_list(conn)
        finally:
            conn.close()
    else:
        regs = []

    db_size_bytes = (
        sum(f.stat().st_size for f in settings.lancedb_path.rglob("*") if f.is_file())
        if settings.lancedb_path.exists()
        else 0
    )

    return json.dumps(
        {
            "document_count": len(docs),
            "collection_count": len(cols),
            "chunk_count": chunks,
            "registered_directories": len(regs),
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def list_databases() -> str:
    """List all named databases with document counts and storage size.

    Discovers databases under the quarry data root directory.
    Each database is a named directory containing a LanceDB store.
    """
    settings = _settings()
    databases = discover_databases(settings.quarry_root)
    return json.dumps(
        {
            "current_database": _db_name or "default",
            "total_databases": len(databases),
            "databases": databases,
        },
        indent=2,
    )


@mcp.tool()
@_handle_errors
def use_database(name: str) -> str:
    """Switch to a different named database for subsequent operations.

    All tools (search, ingest, sync, etc.) will use the selected database
    until changed again. Use list_databases to see available databases.

    Args:
        name: Database name (e.g., 'coding', 'work'). Use 'default' for
              the default database.
    """
    global _db_name
    previous = _db_name or "default"
    new_name = name if name != "default" else None
    # Validate before mutating: resolve_db_paths raises ValueError for
    # names containing path separators or traversal segments.
    test_settings = resolve_db_paths(load_settings(), new_name)
    _db_name = new_name
    return json.dumps(
        {
            "previous_database": previous,
            "current_database": name,
            "database_path": str(test_settings.lancedb_path),
        },
        indent=2,
    )


def main(db_name: str | None = None) -> None:
    global _db_name
    _db_name = db_name
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
