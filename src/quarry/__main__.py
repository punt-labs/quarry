from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress

from quarry.config import get_settings
from quarry.database import (
    delete_document as db_delete_document,
    get_db,
    list_documents,
    search,
)
from quarry.embeddings import embed_query
from quarry.pipeline import ingest_document

app = typer.Typer(help="quarry: extract searchable knowledge from any document")
console = Console()


@app.command()
def ingest(
    file_path: Annotated[Path, typer.Argument(help="Path to PDF file")],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace existing data")
    ] = False,
) -> None:
    """Ingest a PDF: analyze, OCR, chunk, embed, and store."""
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    db = get_db(settings.lancedb_path)

    with Progress(console=console) as progress:
        task = progress.add_task(f"Processing {file_path.name}", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        result = ingest_document(
            file_path.resolve(),
            db,
            settings,
            overwrite=overwrite,
            progress_callback=on_progress,
        )

    console.print()
    console.print(json.dumps(result, indent=2))


@app.command(name="search")
def search_cmd(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 10,
) -> None:
    """Search indexed documents."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)

    query_vector = embed_query(query, model_name=settings.embedding_model)
    results = search(db, query_vector, limit=limit)

    for r in results:
        similarity = round(1 - float(str(r.get("_distance", 0))), 4)
        print(
            f"\n[{r['document_name']} p.{r['page_number']}] (similarity: {similarity})"
        )
        text = str(r["text"])
        print(text[:300])


@app.command(name="list")
def list_cmd() -> None:
    """List all indexed documents."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)
    docs = list_documents(db)

    if not docs:
        print("No documents indexed.")
        return

    for doc in docs:
        print(
            f"{doc['document_name']}: "
            f"{doc['indexed_pages']}/{doc['total_pages']} pages, "
            f"{doc['chunk_count']} chunks"
        )


@app.command(name="delete")
def delete_cmd(
    document_name: Annotated[str, typer.Argument(help="Document name to delete")],
) -> None:
    """Delete all indexed data for a document."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)
    deleted = db_delete_document(db, document_name)

    if deleted == 0:
        print(f"No data found for {document_name!r}")
    else:
        print(f"Deleted {deleted} chunks for {document_name!r}")


@app.command()
def mcp() -> None:
    """Start the MCP server (stdio transport)."""
    from quarry.mcp_server import main as mcp_main  # noqa: PLC0415

    mcp_main()


if __name__ == "__main__":
    app()
