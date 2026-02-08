from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from quarry.config import Settings, get_settings
from quarry.database import (
    get_db,
    get_page_text,
    list_documents,
    search,
)
from quarry.embeddings import embed_query
from quarry.pipeline import ingest_document
from quarry.types import LanceDB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("quarry-mcp")


def _settings() -> Settings:
    return get_settings()


def _db() -> LanceDB:
    return get_db(_settings().lancedb_path)


@mcp.tool()
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
def ingest(
    file_path: str,
    overwrite: bool = False,
) -> str:
    """Ingest a PDF document: OCR, chunk, embed, and index for search.

    Args:
        file_path: Absolute path to the PDF file.
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
def get_documents() -> str:
    """List all indexed documents with metadata."""
    db = _db()
    docs = list_documents(db)
    return json.dumps({"total_documents": len(docs), "documents": docs}, indent=2)


@mcp.tool()
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
