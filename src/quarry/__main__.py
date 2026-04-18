from __future__ import annotations

import contextlib
import functools
import http.client
import importlib.metadata
import json
import logging
import os
import ssl
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Any

import typer
import typer.core
from rich.console import Console
from rich.progress import Progress

from quarry.backends import get_embedding_backend
from quarry.collections import derive_collection
from quarry.config import (
    DEFAULT_PORT,
    Settings,
    load_settings,
    read_default_db,
    resolve_db_paths,
    write_default_db,
)
from quarry.database import (
    count_chunks,
    delete_collection as db_delete_collection,
    delete_document as db_delete_document,
    dir_size_bytes,
    discover_databases,
    get_db,
    get_page_text,
    hybrid_search,
    list_collections as db_list_collections,
    list_documents,
)
from quarry.formatting import (
    format_collections,
    format_document_detail,
    format_documents,
    format_status,
)
from quarry.logging_config import configure_logging
from quarry.pipeline import ingest_auto, ingest_content, ingest_document
from quarry.provider import provider_display
from quarry.remote import (
    CA_CERT_PATH,
    MCP_PROXY_CONFIG_PATH,
    PermissionWarning,
    delete_proxy_config,
    fetch_ca_cert,
    mask_token,
    read_proxy_config,
    store_ca_cert,
    validate_connection,
    validate_connection_from_ws_url,
    write_proxy_config,
    ws_to_http,
)
from quarry.sync import sync_all
from quarry.sync_registry import (
    deregister_directory,
    get_registration,
    list_registrations,
    open_registry,
    register_directory,
)
from quarry.tls import TLS_DIR, cert_fingerprint

configure_logging(stderr_level="WARNING")
logger = logging.getLogger(__name__)

_COMMAND_ORDER: list[str] = [
    # Product commands
    "find",
    "ingest",
    "show",
    "remember",
    "status",
    "use",
    "delete",
    "register",
    "deregister",
    "sync",
    "optimize",
    "login",
    "logout",
    "remote",
    "list",
    # Admin commands
    "install",
    "doctor",
    "serve",
    "mcp",
    "version",
    "uninstall",
]


class _OrderedGroup(typer.core.TyperGroup):
    """Typer group that enforces a fixed command order in --help."""

    def list_commands(self, ctx: typer.Context) -> list[str]:  # type: ignore[override]
        commands = super().list_commands(ctx)
        order = {name: i for i, name in enumerate(_COMMAND_ORDER)}
        return sorted(commands, key=lambda c: order.get(c, 999))


app = typer.Typer(
    help="quarry: extract searchable knowledge from any document",
    rich_markup_mode=None,
    cls=_OrderedGroup,
)
hooks_app = typer.Typer(
    help="Claude Code hook handlers (called by hook scripts)",
    rich_markup_mode=None,
)
app.add_typer(hooks_app, name="hooks", hidden=True)
console = Console()
err_console = Console(stderr=True)

# Global state set by @app.callback
_json_output: bool = False
_verbose: bool = False
_quiet: bool = False
_global_db: str = ""


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version as get_version  # noqa: PLC0415

        print(f"quarry {get_version('punt-quarry')}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
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


# ---------------------------------------------------------------------------
# Remote helpers
# ---------------------------------------------------------------------------


class RemoteError(RuntimeError):
    """Error from the remote quarry server with HTTP status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


_DEFAULT_REMOTE_TIMEOUT = 15.0


def _remote_https_request(
    method: str,
    path: str,
    config: dict[str, object],
    body: dict[str, object] | None = None,
    *,
    timeout: float = _DEFAULT_REMOTE_TIMEOUT,
) -> dict[str, object]:
    """Make an authenticated HTTP request to the remote quarry server.

    Derives the HTTPS base URL from the wss:// URL in ``config``, builds a
    pinned SSL context using the CA cert when provided, and returns the parsed
    JSON response body.

    Args:
        method: HTTP method (GET, POST, DELETE).
        path: Request path including query string, e.g. ``/search?q=foo&limit=10``.
        config: Dict from ``read_proxy_config()`` with keys ``url``, optional
            ``ca_cert``, and ``headers``.
        body: Optional JSON-serialisable dict sent as the request body.
        timeout: Socket timeout in seconds.  Defaults to 15; long-running
            operations like ``/sync`` should pass a larger value.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        RemoteError: If the server returns a non-2xx status code, a
            non-JSON / non-dict response body, or if the connection cannot
            be established (status 0 for connection failures).
        SystemExit: If the remote URL uses HTTPS but no CA cert is pinned.
    """
    raw_url = str(config["url"])
    http_base = ws_to_http(raw_url)
    parsed = urllib.parse.urlparse(http_base)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8420

    ca_cert = config.get("ca_cert")
    scheme = "https" if raw_url.startswith("wss://") else "http"
    if scheme == "https" and not ca_cert:
        raise SystemExit(
            "Remote server uses HTTPS but no CA cert is pinned. "
            "Run 'quarry login' to trust the server's certificate."
        )

    headers_raw = config.get("headers", {})
    headers: dict[str, str] = (
        {k: str(v) for k, v in headers_raw.items()}
        if isinstance(headers_raw, dict)
        else {}
    )

    encoded_body: bytes | None = None
    if body is not None:
        encoded_body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    conn: http.client.HTTPConnection | http.client.HTTPSConnection
    if scheme == "https":
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            ssl_ctx.load_verify_locations(str(ca_cert))
        except (OSError, ssl.SSLError) as exc:
            raise SystemExit(
                f"Cannot load CA certificate {ca_cert!r}. "
                f"Run 'quarry login' to configure. ({exc})"
            ) from exc
        conn = http.client.HTTPSConnection(host, port, context=ssl_ctx, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=encoded_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        if resp.status >= 300:
            body_text = resp_body.decode("utf-8", errors="replace")
            raise RemoteError(
                resp.status,
                f"Remote quarry server returned HTTP {resp.status}: {body_text}",
            )
        if not resp_body:
            return {}
        try:
            parsed_body = json.loads(resp_body)
        except json.JSONDecodeError as exc:
            preview = resp_body[:200].decode("utf-8", errors="replace")
            raise RemoteError(
                resp.status,
                f"Remote quarry server returned non-JSON response: {preview!r}",
            ) from exc
        if not isinstance(parsed_body, dict):
            raise RemoteError(
                resp.status,
                f"Malformed response from remote server: "
                f"expected JSON object, got {type(parsed_body).__name__}",
            )
        response_data: dict[str, object] = parsed_body
        return response_data
    except OSError as exc:
        raise RemoteError(
            0,
            f"Cannot connect to remote quarry server at {host}:{port}: {exc}",
        ) from exc
    finally:
        conn.close()


def _remote_https_get(path: str, config: dict[str, object]) -> dict[str, object]:
    """Make an authenticated GET request to the remote quarry server."""
    return _remote_https_request("GET", path, config)


# ---------------------------------------------------------------------------
# Product commands — ordered: find, ingest, show, remember, status, use,
# delete, register, deregister, sync, list
# ---------------------------------------------------------------------------


def _safe_proxy_config() -> dict[str, Any]:
    """Return parsed proxy config, falling back to {} on malformed TOML."""
    try:
        return read_proxy_config()
    except ValueError as exc:
        err_console.print(f"Warning: {exc}", style="yellow")
        return {}


def _find_remote(
    proxy_config: dict[str, object],
    query: str,
    limit: int,
    collection: str,
    document: str,
    page_type: str,
    source_format: str,
    agent_handle: str,
    memory_type: str,
) -> None:
    """Execute a remote find and emit results, exiting 1 on remote request failure."""
    params: dict[str, str | int] = {"q": query, "limit": limit}
    if collection:
        params["collection"] = collection
    if document:
        params["document"] = document
    if page_type:
        params["page_type"] = page_type
    if source_format:
        params["source_format"] = source_format
    if agent_handle:
        params["agent_handle"] = agent_handle
    if memory_type:
        params["memory_type"] = memory_type
    qs = urllib.parse.urlencode(params)
    try:
        remote_resp = _remote_https_get(f"/search?{qs}", proxy_config)
    except RemoteError as exc:
        err_console.print(f"Error: {exc}", style="red")
        raise typer.Exit(code=1) from exc
    raw_results = remote_resp.get("results", [])
    remote_results: list[dict[str, object]] = (
        list(raw_results) if isinstance(raw_results, list) else []
    )
    json_results: list[dict[str, object]] = []
    lines: list[str] = []
    for r in remote_results:
        similarity = round(float(str(r.get("similarity", 0))), 4)
        meta = f"{r.get('page_type', '')}/{r.get('source_format', '')}"
        doc = r.get("document_name", "")
        pg = r.get("page_number", "")
        lines.append(f"\n[{doc} p.{pg} | {meta}] (similarity: {similarity})")
        text = str(r.get("text", ""))
        lines.append(text[:300])
        json_results.append(
            {
                "document_name": r.get("document_name", ""),
                "collection": r.get("collection", ""),
                "page_number": r.get("page_number", 0),
                "chunk_index": r.get("chunk_index", 0),
                "page_type": r.get("page_type", ""),
                "source_format": r.get("source_format", ""),
                "agent_handle": r.get("agent_handle", ""),
                "memory_type": r.get("memory_type", ""),
                "summary": r.get("summary", ""),
                "similarity": similarity,
                "text": text,
            }
        )
    _emit(json_results, "\n".join(lines))


def _exit_on_ingest_failure(result: dict[str, object] | object) -> None:
    """Exit 1 if *result* reports errors and zero ingested chunks.

    Both the local pipeline and the remote HTTP response use the same
    ``{errors, chunks}`` shape.  A successful operation may report errors
    alongside a positive chunk count (partial success); only the "all or
    nothing" failure case is promoted to a non-zero exit code here.
    """
    if not isinstance(result, dict):
        return
    errors_raw = result.get("errors")
    if not isinstance(errors_raw, list) or not errors_raw:
        return
    chunks_raw = result.get("chunks", 0)
    try:
        chunks = int(chunks_raw) if isinstance(chunks_raw, int | float | str) else 0
    except (TypeError, ValueError):
        chunks = 0
    if chunks == 0:
        raise typer.Exit(code=1)


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
    agent_handle: Annotated[
        str,
        typer.Option("--agent-handle", help="Filter by agent handle"),
    ] = "",
    memory_type: Annotated[
        str,
        typer.Option("--memory-type", help="Filter by memory type"),
    ] = "",
) -> None:
    """Search indexed documents."""
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        _find_remote(
            proxy_config,
            query=query,
            limit=limit,
            collection=collection,
            document=document,
            page_type=page_type,
            source_format=source_format,
            agent_handle=agent_handle,
            memory_type=memory_type,
        )
        return

    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    query_vector = get_embedding_backend(settings).embed_query(query)
    results = hybrid_search(
        db,
        query,
        query_vector,
        limit=limit,
        document_filter=document or None,
        collection_filter=collection or None,
        page_type_filter=page_type or None,
        source_format_filter=source_format or None,
        agent_handle_filter=agent_handle or None,
        memory_type_filter=memory_type or None,
    )

    local_json_results: list[dict[str, object]] = []
    local_lines: list[str] = []
    for row in results:
        similarity = round(1 - float(str(row.get("_distance", 0))), 4)
        meta = f"{row['page_type']}/{row['source_format']}"
        local_lines.append(
            f"\n[{row['document_name']} p.{row['page_number']} | {meta}]"
            f" (similarity: {similarity})"
        )
        text = str(row["text"])
        local_lines.append(text[:300])
        local_json_results.append(
            {
                "document_name": row["document_name"],
                "collection": row.get("collection", ""),
                "page_number": row["page_number"],
                "chunk_index": row.get("chunk_index", 0),
                "page_type": row["page_type"],
                "source_format": row["source_format"],
                "agent_handle": row.get("agent_handle", ""),
                "memory_type": row.get("memory_type", ""),
                "summary": row.get("summary", ""),
                "similarity": similarity,
                "text": text,
            }
        )

    _emit(local_json_results, "\n".join(local_lines))


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
    agent_handle: Annotated[
        str,
        typer.Option("--agent-handle", help="Agent handle to tag content"),
    ] = "",
    memory_type: Annotated[
        str,
        typer.Option(
            "--memory-type",
            help="Memory type: fact, observation, opinion, procedure",
        ),
    ] = "",
    summary: Annotated[
        str,
        typer.Option("--summary", help="One-line summary of the content"),
    ] = "",
) -> None:
    """Ingest a file or URL into the knowledge base.

    Auto-detects the source type: URLs (http/https) use smart sitemap
    discovery with single-page fallback; local paths are ingested as files.

    Supports PDF, images, PPTX, XLSX, CSV, HTML, TXT, MD, TEX, DOCX, and
    source code files.
    """
    is_url = source.startswith(("http://", "https://"))

    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        if not is_url:
            err_console.print(
                "Error: file upload to remote server is not supported in "
                "this release. Use a URL or run quarry locally.",
                style="red",
            )
            raise typer.Exit(code=1)
        body: dict[str, object] = {
            "source": source,
            "overwrite": overwrite,
            "collection": collection,
            "agent_handle": agent_handle,
            "memory_type": memory_type,
            "summary": summary,
        }
        try:
            remote_resp = _remote_https_request(
                "POST", "/ingest", proxy_config, body=body
            )
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        _emit(remote_resp, json.dumps(remote_resp, indent=2))
        remote_errors_raw = remote_resp.get("errors", [])
        if isinstance(remote_errors_raw, list):
            for err_msg in remote_errors_raw:
                err_console.print(f"  {err_msg}", style="red")
        _exit_on_ingest_failure(remote_resp)
        return

    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    if is_url:
        with _progress(f"Fetching {source}") as cb:
            result = ingest_auto(
                source,
                db,
                settings,
                overwrite=overwrite,
                collection=collection,
                progress_callback=cb,
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )

        _emit(result, json.dumps(result, indent=2))
        errors: list[str] = result.get("errors", [])  # type: ignore[assignment]
        for err in errors:
            err_console.print(f"  {err}", style="red")
        _exit_on_ingest_failure(result)
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
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )

        _emit(result, json.dumps(result, indent=2))
        _exit_on_ingest_failure(result)


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
    if page is not None and page < 1:
        err_console.print(
            f"Error: page number must be >= 1, got {page}",
            style="red",
        )
        raise typer.Exit(code=1)

    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        params: dict[str, str] = {"document": document_name}
        if page is not None:
            params["page"] = str(page)
        if collection:
            params["collection"] = collection
        qs = urllib.parse.urlencode(params)
        try:
            remote_resp = _remote_https_get(f"/show?{qs}", proxy_config)
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        # If page was requested, format as page text
        if page is not None:
            _emit(
                remote_resp,
                f"Document: {remote_resp.get('document_name', '')}\n"
                f"Page: {remote_resp.get('page_number', '')}\n---\n"
                f"{remote_resp.get('text', '')}",
            )
        else:
            _emit(remote_resp, format_document_detail(remote_resp))
        return

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
            {"document_name": document_name, "page_number": page, "text": text},
            f"Document: {document_name}\nPage: {page}\n---\n{text}",
        )
        return

    docs = list_documents(db, collection_filter=collection or None)
    match = [d for d in docs if d["document_name"] == document_name]
    if not match:
        err_console.print(f"Document {document_name!r} not found", style="red")
        raise typer.Exit(code=1)
    _emit(match[0], format_document_detail(match[0]))


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
    agent_handle: Annotated[
        str,
        typer.Option("--agent-handle", help="Agent handle to tag content"),
    ] = "",
    memory_type: Annotated[
        str,
        typer.Option(
            "--memory-type",
            help="Memory type: fact, observation, opinion, procedure",
        ),
    ] = "",
    summary: Annotated[
        str,
        typer.Option("--summary", help="One-line summary of the content"),
    ] = "",
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

    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        body: dict[str, object] = {
            "name": name,
            "content": content,
            "collection": collection,
            "format_hint": format_hint,
            "overwrite": overwrite,
            "agent_handle": agent_handle,
            "memory_type": memory_type,
            "summary": summary,
        }
        try:
            remote_resp = _remote_https_request(
                "POST", "/remember", proxy_config, body=body
            )
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        _emit(remote_resp, json.dumps(remote_resp, indent=2))
        remote_errors_raw = remote_resp.get("errors", [])
        if isinstance(remote_errors_raw, list):
            for err_msg in remote_errors_raw:
                err_console.print(f"  {err_msg}", style="red")
        _exit_on_ingest_failure(remote_resp)
        return

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
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )

    _emit(result, json.dumps(result, indent=2))
    local_errors_raw = result.get("errors")
    if isinstance(local_errors_raw, list):
        for err_msg in local_errors_raw:
            err_console.print(f"  {err_msg}", style="red")
    _exit_on_ingest_failure(result)


@app.command(name="status")
@_cli_errors
def status_cmd() -> None:
    """Show database status: documents, chunks, storage, model info."""
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        try:
            remote_data = _remote_https_get("/status", proxy_config)
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        _emit(remote_data, format_status(remote_data))
        return

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
        dir_size_bytes(settings.lancedb_path) if settings.lancedb_path.exists() else 0
    )

    data = {
        "document_count": len(docs),
        "collection_count": len(cols),
        "chunk_count": chunks,
        "registered_directories": len(regs),
        "database_path": str(settings.lancedb_path),
        "database_size_bytes": db_size_bytes,
        "embedding_model": settings.embedding_model,
        "provider": provider_display(),
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

    Note: database selection is always a client-side preference.  When a
    remote server is configured, this still writes the local config — the
    remote server is fixed to the database it was started with.
    """
    # Validate the name before persisting.
    resolve_db_paths(load_settings(), name if name != "default" else None)
    write_default_db(name)

    proxy_config = _safe_proxy_config().get("quarry", {})
    remote_note = (
        "Note: this is a client-side preference; "
        "the remote server is fixed to its own database."
        if isinstance(proxy_config, dict) and "url" in proxy_config
        else ""
    )
    text = f"Default database set to {name!r}"
    if remote_note:
        text += f"\n{remote_note}"
    _emit({"database": name}, text)


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
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        if kind == "collection":
            path = f"/collections?name={urllib.parse.quote(name)}"
        elif kind == "document":
            del_params: dict[str, str] = {"name": name}
            if collection:
                del_params["collection"] = collection
            path = f"/documents?{urllib.parse.urlencode(del_params)}"
        else:
            err_console.print(
                f"Error: unknown type {kind!r}. Use 'document' or 'collection'.",
                style="red",
            )
            raise typer.Exit(code=1)
        label = f"collection {name!r}" if kind == "collection" else f"{name!r}"
        try:
            remote_resp = _remote_https_request("DELETE", path, proxy_config)
        except RemoteError as exc:
            if exc.status == 404:
                err_console.print(f"No data found for {label}", style="red")
                raise typer.Exit(code=1) from exc
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        deleted = remote_resp.get("deleted", 0)
        _emit(
            remote_resp,
            f"Deleted {deleted} chunks for {label}",
        )
        return

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
        err_console.print(f"No data found for {label}", style="red")
        raise typer.Exit(code=1)
    _emit(
        {"deleted": deleted, "name": name, "type": kind},
        f"Deleted {deleted} chunks for {label}",
    )


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
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        # Server-side path allowlist requires an absolute path — the resolved
        # path is what the server will enforce against its $HOME.
        resolved_str = str(directory.expanduser().resolve())
        col = collection or directory.name or Path(resolved_str).name
        body: dict[str, object] = {"directory": resolved_str, "collection": col}
        try:
            remote_resp = _remote_https_request(
                "POST", "/registrations", proxy_config, body=body
            )
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        reg_dir = remote_resp.get("directory", resolved_str)
        reg_col = remote_resp.get("collection", col)
        _emit(
            {"directory": str(reg_dir), "collection": str(reg_col)},
            f"Registered {reg_dir} as collection {reg_col!r}",
        )
        return

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
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        params = {
            "collection": collection,
            "keep_data": "true" if keep_data else "false",
        }
        path = f"/registrations?{urllib.parse.urlencode(params)}"
        try:
            remote_resp = _remote_https_request("DELETE", path, proxy_config)
        except RemoteError as exc:
            if exc.status == 404:
                err_console.print(
                    f"No registration found for {collection!r}", style="red"
                )
                raise typer.Exit(code=1) from exc
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        removed_raw = remote_resp.get("removed", 0)
        removed = int(removed_raw) if isinstance(removed_raw, int | float) else 0
        deleted_chunks_raw = remote_resp.get("deleted_chunks", 0)
        deleted_chunks = (
            int(deleted_chunks_raw)
            if isinstance(deleted_chunks_raw, int | float)
            else 0
        )
        _emit(
            {
                "collection": collection,
                "removed": removed,
                "deleted_chunks": deleted_chunks,
            },
            f"Deregistered collection {collection!r} "
            f"({removed} files, {deleted_chunks} chunks deleted)",
        )
        return

    settings = _resolved_settings()
    conn = open_registry(settings.registry_path)
    try:
        existing = get_registration(conn, collection)
        doc_names = deregister_directory(conn, collection)
    finally:
        conn.close()

    if existing is None:
        err_console.print(f"No registration found for {collection!r}", style="red")
        raise typer.Exit(code=1)

    deleted_chunks = 0
    if not keep_data and doc_names:
        db = get_db(settings.lancedb_path)
        for name in doc_names:
            deleted_chunks += db_delete_document(db, name, collection=collection)
    removed = len(doc_names)
    _emit(
        {
            "collection": collection,
            "removed": removed,
            "deleted_chunks": deleted_chunks,
        },
        f"Deregistered collection {collection!r} "
        f"({removed} files, {deleted_chunks} chunks deleted)",
    )


def _auto_workers(settings: Settings) -> int:  # noqa: ARG001
    """Select worker count based on the active ONNX execution provider.

    Returns 4 when CUDAExecutionProvider is active (GPU-accelerated embedding
    is fast; parsing is the bottleneck and is parallelizable). Returns 1 for
    CPU-only deployments. Respects the QUARRY_PROVIDER environment variable.
    Falls back to 1 on any error (broken runtime, missing dependency, etc.).
    """
    try:
        from quarry.provider import select_provider  # noqa: PLC0415

        return 4 if select_provider().provider == "CUDAExecutionProvider" else 1
    except Exception:  # noqa: BLE001
        return 1


def _format_sync_results(json_data: dict[str, dict[str, object]]) -> str:
    """Format a ``{collection: result}`` mapping as a multi-line summary."""
    lines: list[str] = []
    for col, res in json_data.items():
        ingested = res.get("ingested", 0)
        refreshed = res.get("refreshed", 0)
        deleted = res.get("deleted", 0)
        skipped = res.get("skipped", 0)
        failed = res.get("failed", 0)
        line = (
            f"{col}: {ingested} ingested, {refreshed} refreshed, "
            f"{deleted} deleted, {skipped} unchanged, {failed} failed"
        )
        errors = res.get("errors")
        if isinstance(errors, list) and errors:
            line += "\n" + "\n".join(f"  error: {e}" for e in errors)
        lines.append(line)
    return "\n".join(lines)


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
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        if workers is not None:
            err_console.print(
                "Warning: --workers is ignored when a remote quarry server is "
                "configured",
                style="yellow",
            )
        # Fire-and-forget: POST /sync returns 202 with task_id.
        # The CLI prints the task_id and exits immediately.
        try:
            remote_resp = _remote_https_request(
                "POST",
                "/sync",
                proxy_config,
                body={},
                timeout=_DEFAULT_REMOTE_TIMEOUT,
            )
        except RemoteError as exc:
            # 409 means sync already running — extract task_id from the
            # JSON body so the user can poll it.
            if exc.status == 409:
                conflict_task_id = "unknown"
                msg = str(exc)
                # The message embeds the raw body after the HTTP status prefix.
                body_start = msg.find("{")
                if body_start != -1:
                    try:
                        data = json.loads(msg[body_start:])
                        conflict_task_id = str(data.get("task_id", "unknown"))
                    except (json.JSONDecodeError, AttributeError):
                        pass
                err_console.print(
                    f"Sync already in progress: task_id={conflict_task_id}",
                    style="yellow",
                )
                raise typer.Exit(code=0) from exc
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        task_id = remote_resp.get("task_id", "")
        status = remote_resp.get("status", "")
        _emit(
            remote_resp,
            f"Sync {status}: task_id={task_id}",
        )
        return

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

    json_data: dict[str, dict[str, object]] = {
        col: {
            "ingested": res.ingested,
            "refreshed": res.refreshed,
            "deleted": res.deleted,
            "skipped": res.skipped,
            "failed": res.failed,
            "errors": list(res.errors),
        }
        for col, res in results.items()
    }
    _emit(json_data, _format_sync_results(json_data))


@app.command(name="optimize")
@_cli_errors
def optimize_cmd(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Bypass the fragment-count safety guard.",
        ),
    ] = False,
) -> None:
    """Compact the LanceDB table and rebuild indexes.

    When the database has more than 10,000 fragments, optimization is
    skipped by default to prevent a compaction death spiral. Use --force
    to bypass this safety guard for manual recovery.
    """
    from quarry.database import (  # noqa: PLC0415
        FRAGMENT_THRESHOLD,
        count_fragments,
        optimize_table as db_optimize_table,
    )

    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    fragments = count_fragments(db)
    if not _quiet:
        err_console.print(f"Fragment count: {fragments}")

    if not force and fragments > FRAGMENT_THRESHOLD:
        err_console.print(
            f"Skipping: {fragments} fragments exceed threshold "
            f"({FRAGMENT_THRESHOLD:,}). Use --force to override.",
            style="yellow",
        )
        raise typer.Exit(code=1)

    if not _quiet:
        err_console.print("Running optimization...")

    db_optimize_table(db, force=force)

    _emit(
        {"optimized": True, "fragments_before": fragments, "force": force},
        "Optimization complete.",
    )


@app.command(name="login")
@_cli_errors
def login_cmd(
    host: Annotated[str, typer.Argument(help="Remote quarry host (hostname or IP)")],
    port: Annotated[int, typer.Option("--port", "-p", help="Port")] = DEFAULT_PORT,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            help="Bearer token for remote server (omit for unauthenticated servers)",
            hide_input=True,
            envvar="QUARRY_API_KEY",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip TOFU confirmation prompt (non-interactive, trust automatically).",  # noqa: E501
        ),
    ] = False,
) -> None:
    """Connect to a remote quarry server using TOFU certificate pinning.

    Fetches the server's CA certificate over HTTPS with SSL verification
    disabled (TOFU bootstrap), displays its fingerprint, prompts for trust
    confirmation, then validates the connection over HTTPS/WSS and writes
    the mcp-proxy config.
    """
    # Step 1: Fetch CA cert over HTTPS with SSL verification disabled (TOFU bootstrap).
    try:
        ca_cert_pem = fetch_ca_cert(host, port)
    except ValueError as exc:
        err_console.print(f"Error: {exc}", style="red")
        raise typer.Exit(code=1) from exc

    # Step 2: Display fingerprint.
    fp = cert_fingerprint(ca_cert_pem)
    err_console.print(f"Server CA fingerprint: {fp}")

    # Step 3: Prompt for trust (skip if --yes).
    if not yes:
        confirmed = typer.confirm("Trust this server?", default=False)
        if not confirmed:
            print("Aborted. Not logged in.")
            raise typer.Exit(code=0)

    # Step 4: Validate connection using a tempfile — store CA cert only on success
    # so a failed validation does not leave an orphaned cert on disk.
    # Two-block pattern: close the fd explicitly if os.fdopen raises (Fix 1).
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".crt")
    tmp_path = Path(tmp_path_str)
    try:
        try:
            tmp_file = os.fdopen(tmp_fd, "wb")
        except BaseException:
            os.close(tmp_fd)
            tmp_path.unlink(missing_ok=True)
            raise
        try:
            with tmp_file:
                tmp_file.write(ca_cert_pem)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        ok, reason = validate_connection(
            host, port, api_key, scheme="https", ca_cert_path=tmp_path_str
        )
        if not ok:
            err_console.print(f"Error: {reason}", style="red")
            raise typer.Exit(code=1)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Step 5: Write mcp-proxy config first, then store the CA cert.
    # This order ensures that if the CA cert write fails, we can roll back
    # the config — the reverse order has no recovery path (Fix 2).
    ws_url = f"wss://{host}:{port}/mcp"
    try:
        write_proxy_config(ws_url, api_key, str(CA_CERT_PATH))
    except PermissionWarning as exc:
        err_console.print(f"Warning: {exc}", style="yellow")
    except OSError as exc:
        err_console.print(
            f"Error: connection succeeded but could not write config to "
            f"{MCP_PROXY_CONFIG_PATH}: {exc}",
            style="red",
        )
        raise typer.Exit(code=1) from exc

    # Step 6: Store CA cert — roll back the config on failure.
    try:
        store_ca_cert(ca_cert_pem)
    except Exception as exc:
        with contextlib.suppress(OSError):
            delete_proxy_config()
        err_console.print(
            f"Error: could not store CA certificate: {exc}",
            style="red",
        )
        raise typer.Exit(code=1) from exc

    # Step 7: Print success.
    _emit(
        {"host": host, "port": port},
        f"Logged in to {host}:{port}. Restart Claude Code to apply.",
    )


@app.command(name="logout")
@_cli_errors
def logout_cmd() -> None:
    """Disconnect from remote quarry server and revert to local daemon."""
    removed = delete_proxy_config()
    if removed:
        _emit(
            {"logged_out": True},
            "Logged out. Restart Claude Code to revert to local daemon.",
        )
    else:
        _emit({"logged_out": False}, "No remote configured.")


remote_app = typer.Typer(
    help="Manage remote quarry server connection.",
    invoke_without_command=True,
    rich_markup_mode=None,
)
app.add_typer(remote_app, name="remote")


@remote_app.callback(invoke_without_command=True)
def remote_callback(ctx: typer.Context) -> None:
    """Manage remote quarry server connection."""
    if ctx.invoked_subcommand is None:
        err_console.print("Error: specify a subcommand — list.", style="red")
        raise typer.Exit(code=1)


@remote_app.command(name="list")
@_cli_errors
def remote_list_cmd(
    ping: Annotated[bool, typer.Option("--ping", help="Check server health")] = False,
) -> None:
    """Show configured remote server."""
    config = _safe_proxy_config()
    quarry_cfg = config.get("quarry", {})
    if not isinstance(quarry_cfg, dict) or not quarry_cfg:
        _emit({"remote": None}, "No remote configured.")
        return
    url = quarry_cfg.get("url", "")
    if not url:
        _emit(
            {
                "configured": False,
                "message": "No remote configured. Run 'quarry login <host>'.",
            },
            "No remote configured. Run 'quarry login <host>'.",
        )
        return
    headers_raw = quarry_cfg.get("headers")
    auth_header = (
        headers_raw.get("Authorization", "") if isinstance(headers_raw, dict) else ""
    ) or ""
    token: str | None = auth_header.removeprefix("Bearer ").strip() or None
    masked = mask_token(token) if token is not None else "(none)"
    ca_cert = quarry_cfg.get("ca_cert") or None
    text = f"Remote: {url}  token: {masked}"
    data: dict[str, object] = {"url": url, "token_prefix": masked}
    if ping:
        if url.startswith("wss://") and not ca_cert:
            ok, reason = False, "wss:// configured but no CA certificate pinned"
        else:
            ok, reason = validate_connection_from_ws_url(
                url, token, ca_cert_path=str(ca_cert) if ca_cert is not None else None
            )
        status = "healthy" if ok else f"unreachable ({reason})"
        text += f"\nHealth: {status}"
        data["health"] = status
    _emit(data, text)


list_app = typer.Typer(
    help="List documents, collections, databases, or registrations.",
    invoke_without_command=True,
    rich_markup_mode=None,
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
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        params: dict[str, str] = {}
        if collection:
            params["collection"] = collection
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        try:
            remote_resp = _remote_https_get(f"/documents{qs}", proxy_config)
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        raw_docs = remote_resp.get("documents", [])
        if not isinstance(raw_docs, list):
            err_console.print(
                "Warning: unexpected response from remote server", style="yellow"
            )
            raw_docs = []
        docs: list[dict[str, object]] = list(raw_docs)
        _emit(docs, format_documents(docs))
        return

    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)
    local_docs = list_documents(db, collection_filter=collection or None)
    _emit(local_docs, format_documents(local_docs))


@list_app.command(name="collections")
@_cli_errors
def list_collections_cmd() -> None:
    """List all collections with document and chunk counts."""
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        try:
            remote_resp = _remote_https_get("/collections", proxy_config)
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        raw_cols = remote_resp.get("collections", [])
        if not isinstance(raw_cols, list):
            err_console.print(
                "Warning: unexpected response from remote server", style="yellow"
            )
            raw_cols = []
        cols: list[dict[str, object]] = list(raw_cols)
        _emit(cols, format_collections(cols))
        return

    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)
    local_cols = db_list_collections(db)
    _emit(local_cols, format_collections(local_cols))


def _format_registrations(regs: list[dict[str, object]]) -> str:
    if not regs:
        return "No registered directories."
    return "\n".join(
        f"{reg.get('collection', '')}: {reg.get('directory', '')}" for reg in regs
    )


@list_app.command(name="registrations")
@_cli_errors
def list_registrations_cmd() -> None:
    """List all registered directories."""
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        try:
            remote_resp = _remote_https_get("/registrations", proxy_config)
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        raw = remote_resp.get("registrations", [])
        if not isinstance(raw, list):
            err_console.print(
                "Warning: unexpected response from remote server", style="yellow"
            )
            raw = []
        remote_regs: list[dict[str, object]] = [
            {
                "collection": entry.get("collection", ""),
                "directory": entry.get("directory", ""),
                "registered_at": entry.get("registered_at", ""),
            }
            for entry in raw
            if isinstance(entry, dict)
        ]
        _emit(remote_regs, _format_registrations(remote_regs))
        return

    settings = _resolved_settings()
    conn = open_registry(settings.registry_path)
    try:
        regs = list_registrations(conn)
    finally:
        conn.close()

    json_data: list[dict[str, object]] = [
        {
            "collection": reg.collection,
            "directory": str(reg.directory),
            "registered_at": reg.registered_at,
        }
        for reg in regs
    ]
    _emit(json_data, _format_registrations(json_data))


def _format_databases(databases: list[dict[str, object]]) -> str:
    if not databases:
        return "No databases found."
    return "\n".join(
        f"{db_info.get('name', '')}: {db_info.get('document_count', 0)} documents, "
        f"{db_info.get('size_description', '')}"
        for db_info in databases
    )


@list_app.command(name="databases")
@_cli_errors
def list_databases_cmd() -> None:
    """List named databases with document counts and storage size.

    When a remote server is configured, shows the single database the
    remote server is fixed to.  The local path scans
    ``~/.punt-labs/quarry/data/`` for every named database.
    """
    proxy_config = _safe_proxy_config().get("quarry", {})
    if isinstance(proxy_config, dict) and "url" in proxy_config:
        try:
            remote_resp = _remote_https_get("/databases", proxy_config)
        except RemoteError as exc:
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        raw = remote_resp.get("databases", [])
        if not isinstance(raw, list):
            err_console.print(
                "Warning: unexpected response from remote server", style="yellow"
            )
            raw = []
        remote_dbs: list[dict[str, object]] = [
            dict(entry) for entry in raw if isinstance(entry, dict)
        ]
        _emit(remote_dbs, _format_databases(remote_dbs))
        return

    settings = _resolved_settings()
    databases = discover_databases(settings.quarry_root)
    local_dbs: list[dict[str, object]] = [dict(db_info) for db_info in databases]
    _emit(local_dbs, _format_databases(local_dbs))


# ---------------------------------------------------------------------------
# Admin commands — install, doctor, serve, mcp, version, uninstall
# ---------------------------------------------------------------------------


@app.command()
def install() -> None:
    """Set up data directory and download embedding model."""
    from quarry.doctor import run_install  # noqa: PLC0415

    exit_code = run_install()
    raise typer.Exit(code=exit_code)


@app.command()
def doctor() -> None:
    """Check environment: Python, data directory, model, imports."""
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
    tls: Annotated[
        bool,
        typer.Option(
            "--tls",
            help=(
                "Enable TLS using certificates from ~/.punt-labs/quarry/tls/. "
                "Run 'quarry install' first to generate certificates."
            ),
        ),
    ] = False,
) -> None:
    """Start the HTTP API server."""
    from quarry.http_server import serve as http_serve  # noqa: PLC0415

    settings = _resolved_settings()
    origins = frozenset(cors_origin) if cors_origin else None

    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    if tls:
        cert_path = TLS_DIR / "server.crt"
        key_path = TLS_DIR / "server.key"
        if not cert_path.exists() or not key_path.exists():
            err_console.print(
                "Error: TLS certificate files not found in "
                f"{TLS_DIR}. Run 'quarry install' first.",
                style="red",
            )
            raise typer.Exit(code=1)
        ssl_certfile = str(cert_path)
        ssl_keyfile = str(key_path)

    http_serve(
        settings,
        port=port,
        host=host,
        api_key=api_key,
        cors_origins=origins,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


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
