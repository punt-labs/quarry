from __future__ import annotations

import functools
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
from quarry.formatting import (
    format_collections,
    format_databases,
    format_delete_summary,
    format_deregister_summary,
    format_documents,
    format_ingest_summary,
    format_register_summary,
    format_registrations,
    format_search_results,
    format_sitemap_summary,
    format_status,
    format_switch_summary,
    format_sync_summary,
)
from quarry.pipeline import (
    ingest_content as pipeline_ingest_content,
    ingest_document,
    ingest_sitemap as pipeline_ingest_sitemap,
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

    return format_search_results(query, formatted)


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

    result = ingest_document(
        path,
        db,
        settings,
        overwrite=overwrite,
        collection=col,
    )

    return format_ingest_summary(result)


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

    result = pipeline_ingest_content(
        content,
        document_name,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        format_hint=format_hint,
    )

    return format_ingest_summary(result)


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

    result = pipeline_ingest_url(
        url,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        document_name=document_name or None,
    )

    return format_ingest_summary(result)


@mcp.tool()
@_handle_errors
def ingest_sitemap(
    url: str,
    collection: str = "",
    include_patterns: str = "",
    exclude_patterns: str = "",
    limit: int = 0,
    overwrite: bool = False,
    workers: int = 4,
    delay: float = 0.5,
) -> str:
    """Crawl a sitemap and ingest all discovered URLs.

    Parses the sitemap XML, discovers all page URLs (following sitemap
    indexes recursively), applies include/exclude filters, skips pages
    unchanged since last ingest (via <lastmod>), and ingests the rest
    in parallel with rate limiting.

    Args:
        url: Sitemap URL (e.g., https://docs.example.com/sitemap.xml).
        collection: Collection name. Defaults to sitemap URL domain.
        include_patterns: Comma-separated URL path globs to include.
        exclude_patterns: Comma-separated URL path globs to exclude.
        limit: Max URLs to ingest (0 = no limit).
        overwrite: Force re-ingest regardless of lastmod.
        workers: Parallel fetch workers (default 4).
        delay: Base delay between fetches in seconds (default 0.5).
    """
    settings = _settings()
    db = _db()

    include = [p.strip() for p in include_patterns.split(",") if p.strip()] or None
    exclude = [p.strip() for p in exclude_patterns.split(",") if p.strip()] or None

    result = pipeline_ingest_sitemap(
        url,
        db,
        settings,
        collection=collection,
        include=include,
        exclude=exclude,
        limit=limit,
        overwrite=overwrite,
        workers=workers,
        delay=delay,
    )

    return format_sitemap_summary(result)


@mcp.tool()
@_handle_errors
def get_documents(collection: str = "") -> str:
    """List all indexed documents with metadata.

    Args:
        collection: Optional collection name to filter by.
    """
    db = _db()
    docs = list_documents(db, collection_filter=collection or None)
    return format_documents(docs)


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
    return format_delete_summary("document", document_name, deleted)


@mcp.tool()
@_handle_errors
def list_collections() -> str:
    """List all collections with document and chunk counts."""
    db = _db()
    cols = db_list_collections(db)
    return format_collections(cols)


@mcp.tool()
@_handle_errors
def delete_collection(collection: str) -> str:
    """Delete all indexed data for a collection.

    Args:
        collection: Collection name to delete.
    """
    db = _db()
    deleted = db_delete_collection(db, collection)
    return format_delete_summary("collection", collection, deleted)


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
    return format_register_summary(reg.directory, reg.collection)


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

    return format_deregister_summary(
        collection,
        len(doc_names),
        data_deleted=not keep_data,
    )


@mcp.tool()
@_handle_errors
def sync_all_registrations() -> str:
    """Sync all registered directories: ingest new/changed, remove deleted."""
    settings = _settings()
    db = _db()

    results = engine_sync_all(db, settings)

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

    return format_sync_summary(
        {"collections_synced": len(results), "results": summary},
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

    return format_status(
        {
            "document_count": len(docs),
            "collection_count": len(cols),
            "chunk_count": chunks,
            "registered_directories": len(regs),
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
        }
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
    return format_databases(databases, current=_db_name or "default")


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
    return format_switch_summary(previous, name, str(test_settings.lancedb_path))


def main(db_name: str | None = None) -> None:
    global _db_name
    _db_name = db_name
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
