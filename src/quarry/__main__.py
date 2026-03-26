from __future__ import annotations

import contextlib
import functools
import importlib.metadata
import json
import logging
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress

from quarry.backends import get_embedding_backend
from quarry.collections import derive_collection
from quarry.config import (
    DEFAULT_PORT,
    Settings,
    configure_logging,
    load_settings,
    read_default_db,
    resolve_db_paths,
    write_default_db,
)
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
from quarry.formatting import format_document_detail, format_status
from quarry.pipeline import ingest_auto, ingest_content, ingest_document
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

# Global state set by @app.callback
_json_output: bool = False
_verbose: bool = False
_quiet: bool = False
_global_db: str = ""


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Verbose output."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress non-essential output."),
    ] = False,
    database: Annotated[
        str,
        typer.Option(
            "--db",
            help="Named database (default: 'default'). "
            "Resolves to ~/.punt-labs/quarry/data/<name>/lancedb.",
        ),
    ] = "",
) -> None:
    """quarry: extract searchable knowledge from any document."""
    global _json_output, _verbose, _quiet, _global_db
    if verbose and quiet:
        err_console.print("Error: --verbose and --quiet are mutually exclusive.")
        raise typer.Exit(code=1)
    _json_output = output_json
    _verbose = verbose  # reserved: commands will use for extra output
    _quiet = quiet  # reserved: commands will use to suppress non-essential output
    _global_db = database
    if ctx.invoked_subcommand is None:
        print(ctx.get_help())
        raise typer.Exit(code=0)


def _emit(data: object, text: str = "") -> None:
    """Output helper: JSON when --json is active, otherwise text.

    When ``--json`` is set, *data* is serialised to stdout as a single JSON
    line.  Otherwise *text* is printed (if non-empty).  Commands should always
    pass both a structured payload and a human-readable string.
    """
    if _json_output:
        json.dump(data, sys.stdout)
        sys.stdout.write("\n")
    elif text:
        print(text)


@contextlib.contextmanager
def _progress(
    label: str,
) -> Generator[Callable[[str], None] | None]:
    """Yield a progress callback, or None in JSON mode.

    In human mode the Rich progress bar is started and guaranteed to stop
    on exit (including exceptions).  In JSON mode nothing is rendered.
    """
    if _json_output:
        yield None
        return
    p = Progress(console=console)
    task = p.add_task(label, total=None)
    p.start()
    try:
        yield lambda message: p.update(task, description=message)
    finally:
        p.stop()


def _resolved_settings(db: str = "") -> Settings:
    """Load settings with --db resolution applied.

    Priority: per-command ``db`` > global ``--db`` flag > persistent default.
    """
    effective = db or _global_db or read_default_db()
    return resolve_db_paths(load_settings(), effective or None)


def _cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
    """Catch exceptions at the CLI boundary, log, and exit with code 1."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> None:
        try:
            fn(*args, **kwargs)
        except (SystemExit, KeyboardInterrupt, typer.Exit):
            raise
        except Exception as exc:
            logger.exception("Command %s failed", fn.__name__)
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc

    return wrapper


@app.command(name="ingest")
@_cli_errors
def ingest_cmd(
    source: Annotated[
        str,
        typer.Argument(help="File path or URL to ingest"),
    ],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace existing data")
    ] = False,
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Collection name")
    ] = "",
) -> None:
    """Ingest a file or URL into the knowledge base.

    Auto-detects the source type: URLs (http/https) use smart sitemap
    discovery with single-page fallback; local paths are ingested as files.

    Supports PDF, images, PPTX, XLSX, CSV, HTML, TXT, MD, TEX, DOCX, and
    source code files.
    """
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)
    is_url = source.startswith(("http://", "https://"))

    if is_url:
        with _progress(f"Fetching {source}") as cb:
            result = ingest_auto(
                source,
                db,
                settings,
                overwrite=overwrite,
                collection=collection,
                progress_callback=cb,
            )

        _emit(result, json.dumps(result, indent=2))
        errors: list[str] = result.get("errors", [])  # type: ignore[assignment]
        for err in errors:
            err_console.print(f"  {err}", style="red")
    else:
        file_path = Path(source).resolve()
        if file_path.is_dir():
            err_console.print(
                f"Error: {source!r} is a directory. "
                "Use 'quarry register' to track directories.",
                style="red",
            )
            raise typer.Exit(code=1)
        col = derive_collection(file_path, explicit=collection or None)

        with _progress(f"Processing {file_path.name}") as cb:
            result = ingest_document(
                file_path,
                db,
                settings,
                overwrite=overwrite,
                collection=col,
                progress_callback=cb,
            )

        _emit(result, json.dumps(result, indent=2))


@app.command()
@_cli_errors
def remember(
    name: Annotated[
        str, typer.Option("--name", "-n", help="Document name (required)")
    ] = "",
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Collection name"),
    ] = "default",
    format_hint: Annotated[
        str,
        typer.Option("--format", help="Format hint: auto, plain, markdown, latex"),
    ] = "auto",
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite/--no-overwrite",
            help="Replace existing document with same name",
        ),
    ] = True,
) -> None:
    """Ingest inline content from stdin.

    Reads text from stdin and indexes it for semantic search. Requires
    --name to set the document name. Overwrites by default; use
    --no-overwrite to skip if the document already exists.

    Examples:
        echo "meeting notes" | quarry remember --name notes.md
        cat README.md | quarry remember --name readme.md --format markdown
    """
    if not name:
        err_console.print("Error: --name is required for remember.", style="red")
        raise typer.Exit(code=1)

    content = sys.stdin.read()
    if not content.strip():
        err_console.print("Error: no content on stdin.", style="red")
        raise typer.Exit(code=1)

    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    result = ingest_content(
        content,
        name,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        format_hint=format_hint,
    )

    _emit(result, json.dumps(result, indent=2))


@app.command(name="find")
@_cli_errors
def find_cmd(
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
) -> None:
    """Search indexed documents."""
    settings = _resolved_settings()
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

    json_results = []
    lines: list[str] = []
    for r in results:
        similarity = round(1 - float(str(r.get("_distance", 0))), 4)
        meta = f"{r['page_type']}/{r['source_format']}"
        lines.append(
            f"\n[{r['document_name']} p.{r['page_number']} | {meta}]"
            f" (similarity: {similarity})"
        )
        text = str(r["text"])
        lines.append(text[:300])
        json_results.append(
            {
                "document_name": r["document_name"],
                "page_number": r["page_number"],
                "page_type": r["page_type"],
                "source_format": r["source_format"],
                "similarity": similarity,
                "text": text,
                "collection": r.get("collection", ""),
            }
        )

    _emit(json_results, "\n".join(lines))


list_app = typer.Typer(
    help="List documents, collections, databases, or registrations.",
    invoke_without_command=True,
)
app.add_typer(list_app, name="list")


@list_app.callback(invoke_without_command=True)
def list_callback(ctx: typer.Context) -> None:
    """List documents, collections, databases, or registrations."""
    if ctx.invoked_subcommand is None:
        err_console.print(
            "Error: specify a noun — documents, collections, "
            "databases, or registrations.",
            style="red",
        )
        raise typer.Exit(code=1)


@list_app.command(name="documents")
@_cli_errors
def list_documents_cmd(
    collection: Annotated[
        str, typer.Option("--collection", "-c", help="Filter by collection")
    ] = "",
) -> None:
    """List all indexed documents."""
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)
    docs = list_documents(db, collection_filter=collection or None)

    text = (
        "\n".join(
            f"[{doc['collection']}] {doc['document_name']}: "
            f"{doc['indexed_pages']}/{doc['total_pages']} pages, "
            f"{doc['chunk_count']} chunks"
            for doc in docs
        )
        if docs
        else "No documents indexed."
    )
    _emit(docs, text)


@app.command(name="show")
@_cli_errors
def show_cmd(
    document_name: Annotated[str, typer.Argument(help="Document name")],
    page: Annotated[
        int | None,
        typer.Option("--page", "-p", help="Page number to display"),
    ] = None,
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Scope to collection"),
    ] = "",
) -> None:
    """Show document metadata or a specific page's text."""
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    if page is not None:
        text = get_page_text(db, document_name, page, collection=collection or None)
        if text is None:
            err_console.print(
                f"No data found for {document_name!r} page {page}",
                style="red",
            )
            raise typer.Exit(code=1)
        _emit(
            {"document_name": document_name, "page": page, "text": text},
            f"Document: {document_name}\nPage: {page}\n---\n{text}",
        )
        return

    docs = list_documents(db, collection_filter=collection or None)
    match = [d for d in docs if d["document_name"] == document_name]
    if not match:
        err_console.print(f"Document {document_name!r} not found", style="red")
        raise typer.Exit(code=1)
    _emit(match[0], format_document_detail(match[0]))


@app.command(name="status")
@_cli_errors
def status_cmd() -> None:
    """Show database status: documents, chunks, storage, model info."""
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    docs = list_documents(db)
    chunks = count_chunks(db)
    cols = db_list_collections(db)

    if settings.registry_path.exists():
        conn = open_registry(settings.registry_path)
        try:
            regs = list_registrations(conn)
        finally:
            conn.close()
    else:
        regs = []

    db_size_bytes = (
        sum(f.stat().st_size for f in settings.lancedb_path.rglob("*") if f.is_file())
        if settings.lancedb_path.exists()
        else 0
    )

    data = {
        "document_count": len(docs),
        "collection_count": len(cols),
        "chunk_count": chunks,
        "registered_directories": len(regs),
        "database_path": str(settings.lancedb_path),
        "database_size_bytes": db_size_bytes,
        "embedding_model": settings.embedding_model,
    }
    _emit(data, format_status(data))


@app.command(name="use")
@_cli_errors
def use_cmd(
    name: Annotated[str, typer.Argument(help="Database name (e.g., 'coding', 'work')")],
) -> None:
    """Set the persistent default database for subsequent commands.

    Use 'default' to reset to the default database. The --db flag
    overrides this per-call.
    """
    # Validate the name before persisting.
    resolve_db_paths(load_settings(), name if name != "default" else None)
    write_default_db(name)
    _emit({"database": name}, f"Default database set to {name!r}")


@app.command(name="delete")
@_cli_errors
def delete_cmd(
    name: Annotated[str, typer.Argument(help="Document or collection name to delete")],
    kind: Annotated[
        str,
        typer.Option("--type", "-t", help="What to delete: 'document' or 'collection'"),
    ] = "document",
    collection: Annotated[
        str,
        typer.Option("--collection", "-c", help="Scope to collection (documents only)"),
    ] = "",
) -> None:
    """Delete indexed data for a document or collection."""
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    if kind == "collection":
        deleted = db_delete_collection(db, name)
        label = f"collection {name!r}"
    elif kind == "document":
        deleted = db_delete_document(db, name, collection=collection or None)
        label = f"{name!r}"
    else:
        err_console.print(
            f"Error: unknown type {kind!r}. Use 'document' or 'collection'.",
            style="red",
        )
        raise typer.Exit(code=1)

    if deleted == 0:
        _emit({"deleted": 0, "name": name, "type": kind}, f"No data found for {label}")
    else:
        _emit(
            {"deleted": deleted, "name": name, "type": kind},
            f"Deleted {deleted} chunks for {label}",
        )


@list_app.command(name="collections")
@_cli_errors
def list_collections_cmd() -> None:
    """List all collections with document and chunk counts."""
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)
    cols = db_list_collections(db)

    text = (
        "\n".join(
            f"{col['collection']}: "
            f"{col['document_count']} documents, "
            f"{col['chunk_count']} chunks"
            for col in cols
        )
        if cols
        else "No collections found."
    )
    _emit(cols, text)


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
    settings = _resolved_settings()
    resolved = directory.resolve()
    col = collection or resolved.name
    conn = open_registry(settings.registry_path)
    try:
        reg = register_directory(conn, resolved, col)
        _emit(
            {"directory": str(reg.directory), "collection": reg.collection},
            f"Registered {reg.directory} as collection {reg.collection!r}",
        )
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
    settings = _resolved_settings()
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
    _emit(
        {"collection": collection, "removed": removed},
        f"Deregistered collection {collection!r} ({removed} files)",
    )


@list_app.command(name="registrations")
@_cli_errors
def list_registrations_cmd() -> None:
    """List all registered directories."""
    settings = _resolved_settings()
    conn = open_registry(settings.registry_path)
    try:
        regs = list_registrations(conn)
    finally:
        conn.close()

    json_data = [
        {"collection": reg.collection, "directory": str(reg.directory)} for reg in regs
    ]
    text = (
        "\n".join(f"{reg.collection}: {reg.directory}" for reg in regs)
        if regs
        else "No registered directories."
    )
    _emit(json_data, text)


def _auto_workers(settings: Settings) -> int:  # noqa: ARG001
    """Select worker count. Local backends are CPU-bound — 1 worker."""
    return 1


@app.command(name="sync")
@_cli_errors
def sync_cmd(
    workers: Annotated[
        int | None,
        typer.Option(
            "--workers",
            "-w",
            help="Parallel workers (default: 1)",
        ),
    ] = None,
) -> None:
    """Sync all registered directories: ingest new/changed, remove deleted."""
    settings = _resolved_settings()
    effective_workers = workers if workers is not None else _auto_workers(settings)
    logger.info("Using %d sync workers", effective_workers)
    db = get_db(settings.lancedb_path)

    with _progress("Syncing") as cb:
        results = sync_all(
            db,
            settings,
            max_workers=effective_workers,
            progress_callback=cb,
        )

    json_data = {
        col: {
            "ingested": res.ingested,
            "deleted": res.deleted,
            "skipped": res.skipped,
            "failed": res.failed,
            "errors": res.errors,
        }
        for col, res in results.items()
    }

    lines: list[str] = []
    for col, res in results.items():
        line = (
            f"{col}: {res.ingested} ingested, {res.deleted} deleted, "
            f"{res.skipped} unchanged, {res.failed} failed"
        )
        if res.errors:
            line += "\n" + "\n".join(f"  error: {e}" for e in res.errors)
        lines.append(line)

    _emit(json_data, "\n".join(lines))


@list_app.command(name="databases")
@_cli_errors
def list_databases_cmd() -> None:
    """List named databases with document counts and storage size."""
    settings = _resolved_settings()
    databases = discover_databases(settings.quarry_root)

    text = (
        "\n".join(
            f"{db_info['name']}: {db_info['document_count']} documents, "
            f"{db_info['size_description']}"
            for db_info in databases
        )
        if databases
        else "No databases found."
    )
    _emit(databases, text)


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
        typer.Option(
            "--port",
            "-p",
            help=f"Port to bind (default: {DEFAULT_PORT}, 0 = OS-assigned).",
        ),
    ] = DEFAULT_PORT,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Address to bind. 127.0.0.1 (default) or 0.0.0.0 for containers.",
        ),
    ] = "127.0.0.1",
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            envvar="QUARRY_API_KEY",
            help="Require Bearer token auth on all endpoints except /health.",
        ),
    ] = None,
    cors_origin: Annotated[
        list[str] | None,
        typer.Option(
            "--cors-origin",
            help="Allowed CORS origin (repeatable). Default: http://localhost.",
        ),
    ] = None,
) -> None:
    """Start the HTTP API server."""
    from quarry.http_server import serve as http_serve  # noqa: PLC0415

    settings = _resolved_settings()
    origins = frozenset(cors_origin) if cors_origin else None
    http_serve(settings, port=port, host=host, api_key=api_key, cors_origins=origins)


@app.command()
def mcp() -> None:
    """Start the MCP server (stdio transport)."""
    from quarry.mcp_server import main as mcp_main  # noqa: PLC0415

    mcp_main(db_name=_global_db or read_default_db())


@app.command()
def version() -> None:
    """Print the quarry version."""
    ver = importlib.metadata.version("punt-quarry")
    _emit({"version": ver}, ver)


@app.command()
@_cli_errors
def uninstall() -> None:
    """Remove quarry system daemon and service file.

    Stops the daemon and removes the launchd plist (macOS) or systemd unit
    (Linux).  Clean inverse of the daemon step in ``quarry install``.
    """
    from quarry.service import uninstall as svc_uninstall  # noqa: PLC0415

    msg = svc_uninstall()
    console.print(msg)


# ---------------------------------------------------------------------------
# Hook subcommands — called by Claude Code hook scripts.
# All hooks are fail-open: exceptions are caught, logged, and the process
# exits 0 so Claude Code is never blocked.
# ---------------------------------------------------------------------------


@hooks_app.command(name="session-start")
def hook_session_start() -> None:
    """SessionStart: auto-register and sync the current repo."""
    from quarry._stdlib import run_hook  # noqa: PLC0415
    from quarry.hooks import handle_session_start  # noqa: PLC0415

    run_hook(handle_session_start)


@hooks_app.command(name="post-web-fetch")
def hook_post_web_fetch() -> None:
    """PostToolUse on WebFetch: auto-ingest fetched URLs."""
    from quarry._stdlib import run_hook  # noqa: PLC0415
    from quarry.hooks import handle_post_web_fetch  # noqa: PLC0415

    run_hook(handle_post_web_fetch)


@hooks_app.command(name="pre-compact")
def hook_pre_compact() -> None:
    """PreCompact: capture compaction summaries."""
    from quarry._stdlib import run_hook  # noqa: PLC0415
    from quarry.hooks import handle_pre_compact  # noqa: PLC0415

    run_hook(handle_pre_compact)


if __name__ == "__main__":
    app()
