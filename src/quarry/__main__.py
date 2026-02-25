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
from quarry.config import Settings, configure_logging, load_settings, resolve_db_paths
from quarry.database import (
    delete_collection as db_delete_collection,
    delete_document as db_delete_document,
    discover_databases,
    get_db,
    list_collections as db_list_collections,
    list_documents,
    search,
)
from quarry.pipeline import ingest_document, ingest_sitemap, ingest_url
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
hooks_app = typer.Typer(help="Claude Code hook handlers (called by hook scripts)")
app.add_typer(hooks_app, name="hooks")
console = Console()
err_console = Console(stderr=True)

DbOption = Annotated[
    str,
    typer.Option(
        "--db",
        help="Named database (default: 'default'). "
        "Resolves to ~/.quarry/data/<name>/lancedb.",
    ),
]


def _resolved_settings(db: str = "") -> Settings:
    """Load settings with --db resolution applied."""
    return resolve_db_paths(load_settings(), db or None)


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


@app.command(name="ingest-file")
@_cli_errors
def ingest_file(
    file_path: Annotated[Path, typer.Argument(help="Path to document file")],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace existing data")
    ] = False,
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Collection name")
    ] = "",
    database: DbOption = "",
) -> None:
    """Ingest a document from a file path.

    Supports PDF, images (PNG, JPG, TIFF, BMP, WebP), presentations (PPTX),
    spreadsheets (XLSX, CSV), HTML, TXT, MD, TEX, DOCX, and source code files.
    """
    settings = _resolved_settings(database)
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


@app.command(name="ingest-url")
@_cli_errors
def ingest_url_cmd(
    url: Annotated[str, typer.Argument(help="HTTP(S) URL to fetch and ingest")],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace existing data")
    ] = False,
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Collection name")
    ] = "default",
    name: Annotated[
        str, typer.Option("--name", "-n", help="Document name (defaults to URL)")
    ] = "",
    database: DbOption = "",
) -> None:
    """Fetch a webpage and ingest its content.

    Downloads the HTML from the given URL, strips boilerplate (nav, scripts,
    etc.), converts to Markdown, and indexes the text for semantic search.
    """
    settings = _resolved_settings(database)
    db = get_db(settings.lancedb_path)

    with Progress(console=console) as progress:
        task = progress.add_task(f"Fetching {url}", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        result = ingest_url(
            url,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            document_name=name or None,
            progress_callback=on_progress,
        )

    console.print()
    console.print(json.dumps(result, indent=2))


@app.command(name="ingest-sitemap")
@_cli_errors
def ingest_sitemap_cmd(
    url: Annotated[str, typer.Argument(help="Sitemap URL to crawl")],
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Collection name (default: domain)"),
    ] = "",
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="URL path glob to include (repeatable)"),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="URL path glob to exclude (repeatable)"),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Max URLs to ingest (0 = no limit)")
    ] = 0,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Force re-ingest regardless of lastmod")
    ] = False,
    workers: Annotated[
        int, typer.Option("--workers", "-w", help="Parallel fetch workers")
    ] = 4,
    delay: Annotated[
        float,
        typer.Option("--delay", help="Base delay between fetches in seconds"),
    ] = 0.5,
    database: DbOption = "",
) -> None:
    """Crawl a sitemap and ingest all discovered URLs.

    Parses the sitemap XML, discovers all page URLs (following sitemap
    indexes recursively), applies include/exclude URL path filters, skips
    pages unchanged since last ingest (via <lastmod>), and ingests the
    rest in parallel.
    """
    settings = _resolved_settings(database)
    db = get_db(settings.lancedb_path)

    with Progress(console=console) as progress:
        task = progress.add_task(f"Crawling {url}", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        result = ingest_sitemap(
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
            progress_callback=on_progress,
        )

    console.print()
    console.print(json.dumps(result, indent=2))
    if result["errors"]:
        for err in result["errors"]:
            err_console.print(f"  {err}", style="red")


@app.command(name="search")
@_cli_errors
def search_cmd(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 10,
    document: Annotated[
        str, typer.Option("--document", "-d", help="Filter by document name")
    ] = "",
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Filter by collection")
    ] = "",
    page_type: Annotated[
        str,
        typer.Option("--page-type", help="Filter by content type (text, code, etc.)"),
    ] = "",
    source_format: Annotated[
        str,
        typer.Option(
            "--source-format", help="Filter by source format (.pdf, .py, etc.)"
        ),
    ] = "",
    database: DbOption = "",
) -> None:
    """Search indexed documents."""
    settings = _resolved_settings(database)
    db = get_db(settings.lancedb_path)

    query_vector = get_embedding_backend(settings).embed_query(query)
    results = search(
        db,
        query_vector,
        limit=limit,
        document_filter=document or None,
        collection_filter=collection or None,
        page_type_filter=page_type or None,
        source_format_filter=source_format or None,
    )

    for r in results:
        similarity = round(1 - float(str(r.get("_distance", 0))), 4)
        meta = f"{r['page_type']}/{r['source_format']}"
        print(
            f"\n[{r['document_name']} p.{r['page_number']} | {meta}]"
            f" (similarity: {similarity})"
        )
        text = str(r["text"])
        print(text[:300])


@app.command(name="list")
@_cli_errors
def list_cmd(
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Filter by collection")
    ] = "",
    database: DbOption = "",
) -> None:
    """List all indexed documents."""
    settings = _resolved_settings(database)
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
    database: DbOption = "",
) -> None:
    """Delete all indexed data for a document."""
    settings = _resolved_settings(database)
    db = get_db(settings.lancedb_path)
    deleted = db_delete_document(db, document_name, collection=collection or None)

    if deleted == 0:
        print(f"No data found for {document_name!r}")
    else:
        print(f"Deleted {deleted} chunks for {document_name!r}")


@app.command(name="collections")
@_cli_errors
def collections_cmd(
    database: DbOption = "",
) -> None:
    """List all collections with document and chunk counts."""
    settings = _resolved_settings(database)
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
    database: DbOption = "",
) -> None:
    """Delete all indexed data for a collection."""
    settings = _resolved_settings(database)
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
    database: DbOption = "",
) -> None:
    """Register a directory for incremental sync."""
    settings = _resolved_settings(database)
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
    database: DbOption = "",
) -> None:
    """Remove a directory registration. Optionally keep indexed data."""
    settings = _resolved_settings(database)
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
def registrations_cmd(
    database: DbOption = "",
) -> None:
    """List all registered directories."""
    settings = _resolved_settings(database)
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


_CLOUD_BACKENDS = frozenset({"textract", "sagemaker"})


def _auto_workers(settings: Settings) -> int:
    """Select worker count based on configured backends.

    Cloud backends (Textract, SageMaker) are network-bound and benefit from
    parallelism.  Local backends are CPU-bound — extra workers just contend.
    """
    if (
        settings.ocr_backend in _CLOUD_BACKENDS
        or settings.embedding_backend in _CLOUD_BACKENDS
    ):
        return 4
    return 1


@app.command(name="sync")
@_cli_errors
def sync_cmd(
    workers: Annotated[
        int | None,
        typer.Option(
            "--workers",
            "-w",
            help="Parallel workers (auto: 4 for cloud backends, 1 for local)",
        ),
    ] = None,
    database: DbOption = "",
) -> None:
    """Sync all registered directories: ingest new/changed, remove deleted."""
    settings = _resolved_settings(database)
    effective_workers = workers if workers is not None else _auto_workers(settings)
    logger.info("Using %d sync workers", effective_workers)
    db = get_db(settings.lancedb_path)

    with Progress(console=console) as progress:
        task = progress.add_task("Syncing", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        results = sync_all(
            db,
            settings,
            max_workers=effective_workers,
            progress_callback=on_progress,
        )

    console.print()
    for col, res in results.items():
        console.print(
            f"{col}: {res.ingested} ingested, {res.deleted} deleted, "
            f"{res.skipped} unchanged, {res.failed} failed"
        )
        for err in res.errors:
            console.print(f"  error: {err}", style="red")


@app.command(name="databases")
@_cli_errors
def databases_cmd(
    database: DbOption = "",
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON array."),
    ] = False,
) -> None:
    """List named databases with document counts and storage size."""
    settings = _resolved_settings(database)
    databases = discover_databases(settings.quarry_root)

    if output_json:
        console.print(json.dumps(databases, indent=2))
        return

    if not databases:
        print("No databases found.")
        return

    for db_info in databases:
        print(
            f"{db_info['name']}: {db_info['document_count']} documents, "
            f"{db_info['size_description']}"
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
@_cli_errors
def serve(
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to bind (0 = OS-assigned)"),
    ] = 0,
    database: DbOption = "",
) -> None:
    """Start the HTTP API server for quarry-menubar."""
    from quarry.http_server import serve as http_serve  # noqa: PLC0415

    settings = _resolved_settings(database)
    http_serve(settings, port=port)


@app.command()
def mcp(
    database: DbOption = "",
) -> None:
    """Start the MCP server (stdio transport)."""
    from quarry.mcp_server import main as mcp_main  # noqa: PLC0415

    mcp_main(db_name=database or None)


# ---------------------------------------------------------------------------
# Hook subcommands — called by Claude Code hook scripts.
# All hooks are fail-open: exceptions are caught, logged, and the process
# exits 0 so Claude Code is never blocked.
# ---------------------------------------------------------------------------


def _run_hook(handler: Callable[[dict[str, object]], dict[str, object]]) -> None:
    """Read stdin JSON, call *handler*, write stdout JSON.  Fail-open."""
    import sys  # noqa: PLC0415

    try:
        raw = sys.stdin.read()
        payload: dict[str, object] = json.loads(raw) if raw.strip() else {}
        result = handler(payload)
        sys.stdout.write(json.dumps(result))
        sys.stdout.write("\n")
    except Exception:
        logger.exception("Hook %s failed (fail-open)", handler.__name__)
        sys.stdout.write("{}\n")


@hooks_app.command(name="session-start")
def hook_session_start() -> None:
    """SessionStart: auto-register and sync the current repo."""
    from quarry.hooks import handle_session_start  # noqa: PLC0415

    _run_hook(handle_session_start)


@hooks_app.command(name="post-web-fetch")
def hook_post_web_fetch() -> None:
    """PostToolUse on WebFetch: auto-ingest fetched URLs."""
    from quarry.hooks import handle_post_web_fetch  # noqa: PLC0415

    _run_hook(handle_post_web_fetch)


@hooks_app.command(name="pre-compact")
def hook_pre_compact() -> None:
    """PreCompact: capture compaction summaries."""
    from quarry.hooks import handle_pre_compact  # noqa: PLC0415

    _run_hook(handle_pre_compact)


if __name__ == "__main__":
    app()
