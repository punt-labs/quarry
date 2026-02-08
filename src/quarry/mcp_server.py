from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from quarry.config import Settings, get_settings
from quarry.database import (
    count_chunks,
    delete_document as db_delete_document,
    get_db,
    get_page_text,
    list_documents,
    search,
)
from quarry.embeddings import embed_query
from quarry.pipeline import ingest_document, ingest_text as pipeline_ingest_text
from quarry.types import LanceDB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("quarry-mcp")


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
    return get_settings()


def _db() -> LanceDB:
    return get_db(_settings().lancedb_path)


@mcp.tool()
@_handle_errors
def search_documents(
    query: str,
    limit: int = 10,
    document_filter: str = "",
) -> str:
    """Search indexed documents using semantic similarity.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 10, max 50).
        document_filter: Optional exact document name to filter by.
    """
    limit = min(limit, 50)
    settings = _settings()
    db = _db()

    query_vector = embed_query(query, model_name=settings.embedding_model)

    results = search(
        db,
        query_vector,
        limit=limit,
        document_filter=document_filter or None,
    )

    formatted = [
        {
            "document_name": r["document_name"],
            "page_number": r["page_number"],
            "chunk_index": r["chunk_index"],
            "text": r["text"],
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
def ingest(
    file_path: str,
    overwrite: bool = False,
) -> str:
    """Ingest a document: OCR, chunk, embed, and index for search.

    Supported formats: PDF, TXT, MD, TEX, DOCX.

    Args:
        file_path: Absolute path to the document file.
        overwrite: If true, replace existing data for this document.
    """
    path = Path(file_path)
    settings = _settings()
    db = _db()

    progress_lines: list[str] = []

    result = ingest_document(
        path,
        db,
        settings,
        overwrite=overwrite,
        progress_callback=progress_lines.append,
    )

    progress_lines.append("")
    progress_lines.append(f"Result: {json.dumps(result, indent=2)}")
    return "\n".join(progress_lines)


@mcp.tool()
@_handle_errors
def ingest_text(
    content: str,
    document_name: str,
    overwrite: bool = False,
    format_hint: str = "auto",
) -> str:
    """Ingest raw text content: chunk, embed, and index for search.

    Args:
        content: The text content to ingest.
        document_name: Name for the document (e.g., 'notes.md').
        overwrite: If true, replace existing data for this document.
        format_hint: Format hint: 'auto', 'plain', 'markdown', 'latex'.
    """
    settings = _settings()
    db = _db()

    progress_lines: list[str] = []

    result = pipeline_ingest_text(
        content,
        document_name,
        db,
        settings,
        overwrite=overwrite,
        format_hint=format_hint,
        progress_callback=progress_lines.append,
    )

    progress_lines.append("")
    progress_lines.append(f"Result: {json.dumps(result, indent=2)}")
    return "\n".join(progress_lines)


@mcp.tool()
@_handle_errors
def get_documents() -> str:
    """List all indexed documents with metadata."""
    db = _db()
    docs = list_documents(db)
    return json.dumps({"total_documents": len(docs), "documents": docs}, indent=2)


@mcp.tool()
@_handle_errors
def get_page(
    document_name: str,
    page_number: int,
) -> str:
    """Retrieve the full raw OCR text for a specific document page.

    Args:
        document_name: Document filename (e.g., 'report.pdf').
        page_number: Page number (1-indexed).
    """
    db = _db()
    text = get_page_text(db, document_name, page_number)

    if text is None:
        return f"No data found for {document_name} page {page_number}"

    return f"Document: {document_name}\nPage: {page_number}\n---\n{text}"


@mcp.tool()
@_handle_errors
def delete_document(document_name: str) -> str:
    """Delete all indexed data for a document.

    Args:
        document_name: Document filename (e.g., 'report.pdf').
    """
    db = _db()
    deleted = db_delete_document(db, document_name)
    return json.dumps(
        {
            "document_name": document_name,
            "chunks_deleted": deleted,
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

    db_size_bytes = (
        sum(f.stat().st_size for f in settings.lancedb_path.rglob("*") if f.is_file())
        if settings.lancedb_path.exists()
        else 0
    )

    return json.dumps(
        {
            "document_count": len(docs),
            "chunk_count": chunks,
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": 768,
        },
        indent=2,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
