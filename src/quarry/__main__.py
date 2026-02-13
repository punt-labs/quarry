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

from quarry.backends import get_embedding_backend
from quarry.collections import derive_collection
from quarry.config import configure_logging, load_settings
from quarry.database import (
    delete_collection as db_delete_collection,
    delete_document as db_delete_document,
    get_db,
    list_collections as db_list_collections,
    list_documents,
    search,
)
from quarry.pipeline import ingest_document
from quarry.sync import sync_all
from quarry.sync_registry import (
    deregister_directory,
    list_registrations,
    open_registry,
    register_directory,
)

configure_logging(load_settings())
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
    settings = load_settings()
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
    settings = load_settings()
    db = get_db(settings.lancedb_path)

    query_vector = get_embedding_backend(settings).embed_query(query)
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
    settings = load_settings()
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
    settings = load_settings()
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
    settings = load_settings()
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


@app.command(name="delete-collection")
@_cli_errors
def delete_collection_cmd(
    collection: Annotated[str, typer.Argument(help="Collection name to delete")],
) -> None:
    """Delete all indexed data for a collection."""
    settings = load_settings()
    db = get_db(settings.lancedb_path)
    deleted = db_delete_collection(db, collection)

    if deleted == 0:
        print(f"No data found for collection {collection!r}")
    else:
        print(f"Deleted {deleted} chunks for collection {collection!r}")


@app.command()
@_cli_errors
def register(
    directory: Annotated[Path, typer.Argument(help="Directory to register")],
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Collection name (default: dir name)"),
    ] = "",
) -> None:
    """Register a directory for incremental sync."""
    settings = load_settings()
    resolved = directory.resolve()
    col = collection or resolved.name
    conn = open_registry(settings.registry_path)
    try:
        reg = register_directory(conn, resolved, col)
        print(f"Registered {reg.directory} as collection {reg.collection!r}")
    finally:
        conn.close()


@app.command()
@_cli_errors
def deregister(
    collection: Annotated[str, typer.Argument(help="Collection to deregister")],
    keep_data: Annotated[
        bool,
        typer.Option("--keep-data", help="Keep indexed data in LanceDB"),
    ] = False,
) -> None:
    """Remove a directory registration. Optionally keep indexed data."""
    settings = load_settings()
    conn = open_registry(settings.registry_path)
    try:
        doc_names = deregister_directory(conn, collection)
    finally:
        conn.close()

    if not keep_data and doc_names:
        db = get_db(settings.lancedb_path)
        for name in doc_names:
            db_delete_document(db, name, collection=collection)
    removed = len(doc_names)
    print(f"Deregistered collection {collection!r} ({removed} files)")


@app.command(name="registrations")
@_cli_errors
def registrations_cmd() -> None:
    """List all registered directories."""
    settings = load_settings()
    conn = open_registry(settings.registry_path)
    try:
        regs = list_registrations(conn)
    finally:
        conn.close()

    if not regs:
        print("No registered directories.")
        return

    for reg in regs:
        print(f"{reg.collection}: {reg.directory}")


@app.command(name="sync")
@_cli_errors
def sync_cmd(
    workers: Annotated[
        int, typer.Option("--workers", "-w", help="Parallel workers")
    ] = 4,
) -> None:
    """Sync all registered directories: ingest new/changed, remove deleted."""
    settings = load_settings()
    db = get_db(settings.lancedb_path)

    with Progress(console=console) as progress:
        task = progress.add_task("Syncing", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        results = sync_all(
            db, settings, max_workers=workers, progress_callback=on_progress
        )

    console.print()
    for col, res in results.items():
        console.print(
            f"{col}: {res.ingested} ingested, {res.deleted} deleted, "
            f"{res.skipped} unchanged, {res.failed} failed"
        )
        for err in res.errors:
            console.print(f"  error: {err}", style="red")


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
