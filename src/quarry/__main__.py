from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress

from quarry.collections import derive_collection
from quarry.config import get_settings
from quarry.database import (
    delete_document as db_delete_document,
    get_db,
    list_collections as db_list_collections,
    list_documents,
    search,
)
from quarry.embeddings import embed_query
from quarry.pipeline import ingest_document

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = typer.Typer(help="quarry: extract searchable knowledge from any document")
console = Console()
err_console = Console(stderr=True)


def _cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
    """Catch exceptions at the CLI boundary, log, and exit with code 1."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.exception("Command %s failed", fn.__name__)
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc

    return wrapper


@app.command()
@_cli_errors
def ingest(
    file_path: Annotated[Path, typer.Argument(help="Path to document file")],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace existing data")
    ] = False,
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Collection name")
    ] = "",
) -> None:
    """Ingest a document: chunk, embed, and store. Supports PDF, TXT, MD, TEX, DOCX."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)
    resolved = file_path.resolve()
    col = derive_collection(resolved, explicit=collection or None)

    with Progress(console=console) as progress:
        task = progress.add_task(f"Processing {file_path.name}", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        result = ingest_document(
            resolved,
            db,
            settings,
            overwrite=overwrite,
            collection=col,
            progress_callback=on_progress,
        )

    console.print()
    console.print(json.dumps(result, indent=2))


@app.command(name="search")
@_cli_errors
def search_cmd(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 10,
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Filter by collection")
    ] = "",
) -> None:
    """Search indexed documents."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)

    query_vector = embed_query(query, model_name=settings.embedding_model)
    results = search(
        db, query_vector, limit=limit, collection_filter=collection or None
    )

    for r in results:
        similarity = round(1 - float(str(r.get("_distance", 0))), 4)
        print(
            f"\n[{r['document_name']} p.{r['page_number']}] (similarity: {similarity})"
        )
        text = str(r["text"])
        print(text[:300])


@app.command(name="list")
@_cli_errors
def list_cmd(
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Filter by collection")
    ] = "",
) -> None:
    """List all indexed documents."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)
    docs = list_documents(db, collection_filter=collection or None)

    if not docs:
        print("No documents indexed.")
        return

    for doc in docs:
        print(
            f"[{doc['collection']}] {doc['document_name']}: "
            f"{doc['indexed_pages']}/{doc['total_pages']} pages, "
            f"{doc['chunk_count']} chunks"
        )


@app.command(name="delete")
@_cli_errors
def delete_cmd(
    document_name: Annotated[str, typer.Argument(help="Document name to delete")],
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Scope to collection")
    ] = "",
) -> None:
    """Delete all indexed data for a document."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)
    deleted = db_delete_document(db, document_name, collection=collection or None)

    if deleted == 0:
        print(f"No data found for {document_name!r}")
    else:
        print(f"Deleted {deleted} chunks for {document_name!r}")


@app.command(name="collections")
@_cli_errors
def collections_cmd() -> None:
    """List all collections with document and chunk counts."""
    settings = get_settings()
    db = get_db(settings.lancedb_path)
    cols = db_list_collections(db)

    if not cols:
        print("No collections found.")
        return

    for col in cols:
        print(
            f"{col['collection']}: "
            f"{col['document_count']} documents, "
            f"{col['chunk_count']} chunks"
        )


@app.command()
def install() -> None:
    """Set up data directory and download embedding model."""
    from quarry.doctor import run_install  # noqa: PLC0415

    exit_code = run_install()
    raise typer.Exit(code=exit_code)


@app.command()
def doctor() -> None:
    """Check environment: Python, AWS, data directory, model, imports."""
    from quarry.doctor import check_environment  # noqa: PLC0415

    exit_code = check_environment()
    raise typer.Exit(code=exit_code)


@app.command()
def mcp() -> None:
    """Start the MCP server (stdio transport)."""
    from quarry.mcp_server import main as mcp_main  # noqa: PLC0415

    mcp_main()


if __name__ == "__main__":
    app()
