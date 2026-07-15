"""ASGI HTTP server exposing the Quarry REST API over localhost.

Designed for the quarry-menubar macOS companion app and production
deployment.  Uses Starlette with uvicorn for async request handling.

Lifecycle:
    1. ``quarry serve`` loads settings + embedding model (cold start)
    2. Writes port to ``~/.punt-labs/quarry/data/<db>/serve.port``
    3. Serves the JSON REST endpoints on ``localhost:<port>``
    4. Cleans up port file on shutdown
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import pwd
import socket as socket_module
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from socket import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from quarry.config import DEFAULT_PORT, Settings
from quarry.db import Database
from quarry.db.storage import dir_size_bytes, format_size
from quarry.fd_telemetry import FdTelemetry
from quarry.http_guards import RequestGuards
from quarry.http_resources import QuarryResources
from quarry.ingestion.provider import ProviderSelection
from quarry.results import SearchFilter
from quarry.retrieval import SearchService
from quarry.sync_registry import DirectoryRegistration, SyncRegistry

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from starlette.requests import Request

    from quarry.types import EmbeddingBackend

logger = logging.getLogger(__name__)

_DEFAULT_CORS_ORIGINS = frozenset({"http://localhost"})

_AUTH_EXEMPT_PATHS = frozenset({"/health", "/ca.crt"})

# Maximum request body sizes.  /remember accepts content, /ingest only a URL.
MAX_REMEMBER_BODY_BYTES = 50 * 1024 * 1024
MAX_INGEST_BODY_BYTES = 1 * 1024 * 1024
# /sync and /registrations bodies carry only small option dicts.
MAX_SYNC_BODY_BYTES = 16 * 1024
MAX_REGISTRATIONS_BODY_BYTES = 16 * 1024

# Hostnames that expose cloud instance metadata services.  Reject regardless
# of DNS resolution to harden against DNS-rebinding and TOCTOU attacks.
_METADATA_HOSTNAMES = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata",
        "instance-data.ec2.internal",
    }
)

# RFC 6598 Shared Address Space (Carrier-Grade NAT).  Python's
# ``ipaddress.ip_address().is_private`` predates RFC 6598 and does not cover
# this range, so we check it explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# Task GC: completed/failed tasks are evicted after this many seconds.
TASK_TTL_SECONDS = 3600  # 1 hour


def _validate_ingest_url(url: str) -> str | None:
    """Return None if *url* is safe to fetch, else a human-readable reason.

    Rejects URLs that resolve to private, loopback, link-local, reserved,
    multicast, or CGNAT (RFC 6598) addresses, ``.local`` hostnames, and
    well-known cloud metadata endpoints.  Scheme and hostname comparisons
    are case-insensitive — ``urlsplit`` normalizes both per RFC 3986.

    Note: This check has a known DNS rebinding race.  The DNS resolution
    here and the one performed by the downstream fetcher are independent.
    An attacker controlling DNS can return a safe public IP here and a
    private or metadata IP during the actual fetch.  Mitigating this
    requires pinning the resolved IP for the fetch, which is tracked as a
    follow-up.  For now, POST /ingest is authenticated-only, so a
    rebinding attack requires a compromised API key.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return f"unsupported scheme {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return "missing hostname"

    host_lower = host.lower()
    if host_lower in _METADATA_HOSTNAMES:
        return f"metadata hostname {host!r} is blocked"
    if host_lower.endswith(".local"):
        return f"'.local' hostname {host!r} is blocked"

    try:
        infos = socket_module.getaddrinfo(host, None)
    except OSError as exc:
        return f"cannot resolve hostname {host!r}: {exc}"

    for info in infos:
        sockaddr = info[4]
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return f"cannot parse resolved address for {host!r}"
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return f"host {host!r} resolves to blocked address {addr}"
        if addr.version == 4 and addr in _CGNAT_NETWORK:
            return f"host {host!r} resolves to CGNAT address {addr}"

    return None


@dataclass
class TaskState:
    """Tracks the state of an in-progress or completed background task."""

    task_id: str
    kind: str  # "sync", "ingest", "remember", "delete", "register", "deregister"
    status: str = "running"
    results: dict[str, object] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.monotonic)


@contextmanager
def _task_terminal(state: TaskState) -> Iterator[None]:
    """Record *state*'s terminal status when its background body exits.

    Cancellation is recorded then re-raised so the event loop still observes
    it; any other exception is logged and recorded as the failure reason; a
    body that exits without setting a terminal status is marked failed so no
    task is ever left stuck in ``running`` (a guard for future code paths).
    """
    try:
        yield
    except asyncio.CancelledError:
        state.status = "failed"
        state.error = "task was cancelled"
        raise
    except Exception as exc:
        logger.exception("Background %s failed", state.kind)
        state.status = "failed"
        state.error = str(exc)
    finally:
        if state.status == "running":
            state.status = "failed"
            state.error = "task exited without setting terminal status"


def _begin_task(ctx: _QuarryContext, kind: str) -> TaskState:
    """Create a TaskState, evicting completed tasks older than the TTL first."""
    now = time.monotonic()
    expired = [
        tid
        for tid, task in ctx.tasks.items()
        if task.status != "running" and (now - task.created_at) > TASK_TTL_SECONDS
    ]
    for tid in expired:
        del ctx.tasks[tid]
        ctx.task_refs.pop(tid, None)
    task_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    state = TaskState(task_id=task_id, kind=kind)
    ctx.tasks[task_id] = state
    return state


def _on_task_done(ctx: _QuarryContext, task_id: str, _task: asyncio.Task[None]) -> None:
    """Remove the asyncio.Task ref when the task reaches a terminal state."""
    ctx.task_refs.pop(task_id, None)


class _QuarryContext:
    """Shared state for the HTTP server: settings, database, embeddings."""

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        cors_origins: frozenset[str] | None = None,
    ) -> None:
        self._resources = QuarryResources(settings)
        self.api_key = api_key
        self.cors_origins = cors_origins or _DEFAULT_CORS_ORIGINS
        self.start_time = time.monotonic()
        self.tasks: dict[str, TaskState] = {}
        self.task_refs: dict[str, asyncio.Task[None]] = {}

    @property
    def database(self) -> Database:
        return self._resources.database

    @property
    def query_database(self) -> Database:
        return self._resources.query_database

    @property
    def embedder(self) -> EmbeddingBackend:
        return self._resources.embedder

    @property
    def settings(self) -> Settings:
        return self._resources.settings

    def warm(self) -> None:
        self._resources.warm()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _check_bearer_auth(api_key: str, auth_header: str) -> bool:
    """Return True if *auth_header* carries the correct Bearer token."""
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1], api_key)


# ---------------------------------------------------------------------------
# REST route handlers
# ---------------------------------------------------------------------------


def _ctx(request: Request) -> _QuarryContext:
    ctx: _QuarryContext = request.app.state.ctx
    return ctx


def _check_auth(request: Request) -> JSONResponse | None:
    """Return a 401 JSONResponse if auth fails, else None."""
    ctx = _ctx(request)
    if not ctx.api_key or request.url.path in _AUTH_EXEMPT_PATHS:
        return None
    if not _check_bearer_auth(ctx.api_key, request.headers.get("Authorization", "")):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def _health_route(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    return JSONResponse(
        {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - ctx.start_time, 1),
        }
    )


def _ca_cert_route(request: Request) -> Response:  # noqa: ARG001
    """Serve the CA certificate PEM for TOFU bootstrap.

    Auth-exempt so the client can fetch it before login.
    Returns 404 with JSON error if no cert file exists.
    """
    from quarry.tls import TLS_DIR  # noqa: PLC0415

    ca_path = TLS_DIR / "ca.crt"
    if not ca_path.exists():
        return JSONResponse(
            {"error": "No CA certificate found. Run 'quarry install' first."},
            status_code=404,
        )
    return PlainTextResponse(
        ca_path.read_text(),
        media_type="application/x-pem-file",
    )


def _search_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "Missing required parameter: q"}, status_code=400)

    try:
        limit = max(1, min(int(request.query_params.get("limit", "10")), 50))
    except ValueError:
        limit = 10

    collection = request.query_params.get("collection") or None
    page_type = request.query_params.get("page_type") or None
    source_format = request.query_params.get("source_format") or None
    document = request.query_params.get("document") or None
    agent_handle = request.query_params.get("agent_handle") or None
    memory_type = request.query_params.get("memory_type") or None

    ctx = _ctx(request)
    db = ctx.query_database
    query_vector = ctx.embedder.embed_query(query)
    search_filter = SearchFilter(
        collection=collection,
        document=document,
        page_type=page_type,
        source_format=source_format,
        agent_handle=agent_handle,
        memory_type=memory_type,
    )
    results = SearchService(db).search(query, query_vector, search_filter, limit)

    formatted = [r.to_dict() for r in results]

    logger.info("Search results=%d", len(formatted))
    return JSONResponse(
        {"query": query, "total_results": len(formatted), "results": formatted}
    )


def _documents_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    collection = request.query_params.get("collection") or None
    ctx = _ctx(request)
    docs = ctx.database.catalog.list_documents(collection_filter=collection)
    return JSONResponse({"total_documents": len(docs), "documents": docs})


async def _documents_delete_route(request: Request) -> JSONResponse:
    """Handle DELETE /documents as an async 202 background task."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    name = request.query_params.get("name", "")
    if not name:
        return JSONResponse(
            {"error": "Missing required parameter: name"}, status_code=400
        )

    collection = request.query_params.get("collection") or None
    ctx = _ctx(request)
    state = _begin_task(ctx, "delete")
    delete_call = partial(
        ctx.database.store.delete_document, name, collection=collection
    )
    task = asyncio.create_task(_run_delete_task(state, delete_call, name, "document"))
    task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
    ctx.task_refs[state.task_id] = task

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted"},
        status_code=202,
    )


async def _run_delete_task(
    state: TaskState,
    delete_call: Callable[[], int],
    name: str,
    kind_label: str,
) -> None:
    """Run *delete_call* in a worker thread and record the deleted count.

    Shared by document and collection deletion, which differ only in the
    store method invoked and the ``type`` label reported to the client.
    """
    with _task_terminal(state):
        count = await run_in_threadpool(delete_call)
        state.status = "completed"
        state.results = {"deleted": count, "name": name, "type": kind_label}


def _collections_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    ctx = _ctx(request)
    cols = ctx.database.catalog.list_collections()
    return JSONResponse({"total_collections": len(cols), "collections": cols})


async def _collections_delete_route(request: Request) -> JSONResponse:
    """Handle DELETE /collections as an async 202 background task."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    name = request.query_params.get("name", "")
    if not name:
        return JSONResponse(
            {"error": "Missing required parameter: name"}, status_code=400
        )

    ctx = _ctx(request)
    state = _begin_task(ctx, "delete")
    delete_call = partial(ctx.database.store.delete_collection, name)
    task = asyncio.create_task(_run_delete_task(state, delete_call, name, "collection"))
    task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
    ctx.task_refs[state.task_id] = task

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted"},
        status_code=202,
    )


def _show_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    document = request.query_params.get("document", "")
    if not document:
        return JSONResponse(
            {"error": "Missing required parameter: document"}, status_code=400
        )

    collection = request.query_params.get("collection") or None
    page_str = request.query_params.get("page")
    page = 0
    if page_str:
        try:
            page = int(page_str)
        except ValueError:
            return JSONResponse(
                {"error": f"Invalid page number: {page_str!r}"},
                status_code=400,
            )
        if page < 1:
            return JSONResponse(
                {"error": f"Invalid page number: {page} (must be >= 1)"},
                status_code=400,
            )

    ctx = _ctx(request)

    if page > 0:
        text = ctx.database.catalog.get_page_text(document, page, collection=collection)
        if text is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(
            {"document_name": document, "page_number": page, "text": text}
        )

    # No page or page == 0: return document metadata.
    docs = ctx.database.catalog.list_documents(collection_filter=collection)
    match = [d for d in docs if d["document_name"] == document]
    if not match:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(match[0])


async def _remember_route(request: Request) -> JSONResponse:
    """Ingest inline text content as a background task.

    Body: {name, content, ...optional}. Returns 202 Accepted with a task_id.
    """
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    size_err = RequestGuards.check_body_size(request, MAX_REMEMBER_BODY_BYTES)
    if size_err is not None:
        return size_err

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "Missing required field: name"}, status_code=400)
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse(
            {"error": "Missing required field: content"}, status_code=400
        )

    collection = body.get("collection") or "default"
    format_hint = body.get("format_hint") or "auto"
    overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=True)
    if isinstance(overwrite, JSONResponse):
        return overwrite
    agent_handle = body.get("agent_handle") or ""
    memory_type = body.get("memory_type") or ""
    summary = body.get("summary") or ""

    ctx = _ctx(request)
    state = _begin_task(ctx, "remember")
    task = asyncio.create_task(
        _run_remember_task(
            ctx,
            state,
            name=name,
            content=content,
            collection=str(collection),
            format_hint=str(format_hint),
            overwrite=overwrite,
            agent_handle=str(agent_handle),
            memory_type=str(memory_type),
            summary=str(summary),
        ),
    )
    task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
    ctx.task_refs[state.task_id] = task

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted"},
        status_code=202,
    )


async def _run_remember_task(
    ctx: _QuarryContext,
    state: TaskState,
    *,
    name: str,
    content: str,
    collection: str,
    format_hint: str,
    overwrite: bool,
    agent_handle: str,
    memory_type: str,
    summary: str,
) -> None:
    """Execute ingest_content in a background thread and update task state."""
    from quarry.ingestion.pipeline import ingest_content  # noqa: PLC0415

    with _task_terminal(state):
        result = await run_in_threadpool(
            ingest_content,
            content,
            name,
            ctx.database,
            ctx.settings,
            overwrite=overwrite,
            collection=collection,
            format_hint=format_hint,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
        state.status = "completed"
        state.results = dict(result)


async def _ingest_route(request: Request) -> JSONResponse:
    """Ingest a URL as a background task.

    Body: {source, ...optional}. File upload not supported.
    Returns 202 Accepted immediately with a task_id; the actual ingest
    runs as an asyncio background task.  ``GET /tasks/<task_id>`` returns
    the task status.  Unlike sync, multiple concurrent ingests are allowed.
    """
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    size_err = RequestGuards.check_body_size(request, MAX_INGEST_BODY_BYTES)
    if size_err is not None:
        return size_err

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    source = body.get("source")
    if not isinstance(source, str) or not source:
        return JSONResponse(
            {"error": "Missing required field: source"}, status_code=400
        )

    # _validate_ingest_url owns all scheme validation.  It calls
    # getaddrinfo(), which can block on DNS — run it in the threadpool.
    reason = await run_in_threadpool(_validate_ingest_url, source)
    if reason is not None:
        return JSONResponse(
            {"error": f"URL rejected: {reason}"},
            status_code=400,
        )

    overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=False)
    if isinstance(overwrite, JSONResponse):
        return overwrite
    collection = body.get("collection") or ""
    agent_handle = body.get("agent_handle") or ""
    memory_type = body.get("memory_type") or ""
    summary = body.get("summary") or ""

    ctx = _ctx(request)
    state = _begin_task(ctx, "ingest")
    task = asyncio.create_task(
        _run_ingest_task(
            ctx,
            state,
            source=source,
            overwrite=overwrite,
            collection=str(collection),
            agent_handle=str(agent_handle),
            memory_type=str(memory_type),
            summary=str(summary),
        ),
    )
    task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
    ctx.task_refs[state.task_id] = task

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted"},
        status_code=202,
    )


async def _run_ingest_task(
    ctx: _QuarryContext,
    state: TaskState,
    *,
    source: str,
    overwrite: bool,
    collection: str,
    agent_handle: str,
    memory_type: str,
    summary: str,
) -> None:
    """Execute ingest_auto in a background thread and update task state."""
    from quarry.ingestion.pipeline import ingest_auto  # noqa: PLC0415

    with _task_terminal(state):
        result = await run_in_threadpool(
            ingest_auto,
            source,
            ctx.database,
            ctx.settings,
            overwrite=overwrite,
            collection=collection,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
        state.status = "completed"
        state.results = dict(result)


async def _run_sync_task(ctx: _QuarryContext, state: TaskState) -> None:
    """Execute sync_all in a background thread and update *state*."""
    from quarry.sync import sync_all  # noqa: PLC0415

    with _task_terminal(state):
        results = await run_in_threadpool(sync_all, ctx.database.db, ctx.settings)
        state.status = "completed"
        state.results = {
            collection: {
                "ingested": res.ingested,
                "refreshed": res.refreshed,
                "deleted": res.deleted,
                "skipped": res.skipped,
                "failed": res.failed,
                "errors": list(res.errors),
            }
            for collection, res in results.items()
        }


async def _sync_route(request: Request) -> JSONResponse:
    """Accept a sync request and run ``sync_all`` as a background task.

    Uses a non-blocking check to reject concurrent requests with HTTP 409.
    Returns 202 Accepted immediately with a task_id; the actual sync runs
    as an asyncio background task.  ``GET /tasks/<task_id>`` returns the
    task status.
    """
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    size_err = RequestGuards.check_body_size(request, MAX_SYNC_BODY_BYTES)
    if size_err is not None:
        return size_err

    # Reject malformed JSON so clients do not think the server silently
    # ignored their arguments.  An empty body is fine.
    if int(request.headers.get("content-length", "0") or "0") > 0:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Body must be a JSON object"}, status_code=400
            )

    ctx = _ctx(request)

    # Reject if a sync task is already running (dict scan).
    running_sync = next(
        (t for t in ctx.tasks.values() if t.kind == "sync" and t.status == "running"),
        None,
    )
    if running_sync is not None:
        return JSONResponse(
            {
                "error": "Sync already in progress",
                "status": "running",
                "task_id": running_sync.task_id,
            },
            status_code=409,
        )

    state = _begin_task(ctx, "sync")
    task = asyncio.create_task(_run_sync_task(ctx, state))
    task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
    ctx.task_refs[state.task_id] = task

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted"},
        status_code=202,
    )


def _run_captures_push(settings: Settings) -> dict[str, dict[str, object]]:
    """Push every enabled project's redacted captures (identical to the CLI path)."""
    from quarry.shadow import CaptureSync  # noqa: PLC0415

    results = CaptureSync.push_registered(settings, fail_open=True)
    return {col: res.to_dict() for col, res in results.items()}


async def _captures_push_route(request: Request) -> JSONResponse:
    """Re-scrub and push enabled capture shadows; mirrors ``quarry captures push``."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp
    size_err = RequestGuards.check_body_size(request, MAX_SYNC_BODY_BYTES)
    if size_err is not None:
        return size_err
    ctx = _ctx(request)
    results = await run_in_threadpool(_run_captures_push, ctx.settings)
    return JSONResponse({"results": results})


def _task_status_route(request: Request) -> JSONResponse:
    """Return the status of any task by task_id.

    Registered on /tasks/{task_id}, /sync/{task_id}, and /ingest/{task_id}
    for backwards compatibility.
    """
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    task_id = request.path_params.get("task_id", "")
    ctx = _ctx(request)

    state = ctx.tasks.get(task_id)
    if state is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    response: dict[str, object] = {
        "task_id": state.task_id,
        "status": state.status,
    }
    if state.status == "completed":
        response["results"] = state.results
    elif state.status == "failed":
        response["error"] = state.error
    return JSONResponse(response)


def _databases_route(request: Request) -> JSONResponse:
    """Return a single-entry list describing the server's configured database.

    The server process is fixed to one database — selection is a client-side
    concern.  The response shape matches ``discover_databases`` so the CLI can
    format remote and local output identically.
    """
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    ctx = _ctx(request)
    settings = ctx.settings
    lance_dir = settings.lancedb_path

    # A fresh database has no ``chunks`` table yet; ``list_documents`` can
    # raise on that path.  Treat any failure as "zero documents" so the
    # remote /databases endpoint keeps the contract of ``discover_databases``.
    if lance_dir.exists():
        try:
            docs = ctx.database.catalog.list_documents()
        except Exception:  # noqa: BLE001 — table may not exist yet
            logger.debug("list_documents failed on fresh database", exc_info=True)
            docs = []
    else:
        docs = []
    size_bytes = dir_size_bytes(lance_dir) if lance_dir.exists() else 0
    name = lance_dir.parent.name or "default"
    summary = {
        "name": name,
        "document_count": len(docs),
        "size_bytes": size_bytes,
        "size_description": format_size(size_bytes),
    }
    return JSONResponse({"total_databases": 1, "databases": [summary]})


def _use_route(request: Request) -> JSONResponse:
    """Reject database selection: the server is fixed to its own database."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    return JSONResponse(
        {
            "error": (
                "database selection is client-side only; "
                "the remote server is fixed to its own database"
            )
        },
        status_code=400,
    )


async def _registrations_route(request: Request) -> JSONResponse:
    """Dispatch GET/POST/DELETE on /registrations to the right handler."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    if request.method == "GET":
        return await _handle_list_registrations(request)
    if request.method == "DELETE":
        return await _handle_delete_registration(request)
    return await _handle_add_registration(request)


def _list_registrations_sync(registry_path: Path) -> list[DirectoryRegistration]:
    """Open registry, list, close — all in one thread."""
    conn = SyncRegistry(registry_path)
    try:
        return conn.list_registrations()
    finally:
        conn.close()


async def _handle_list_registrations(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    settings = ctx.settings
    if not settings.registry_path.exists():
        return JSONResponse({"total_registrations": 0, "registrations": []})

    regs = await run_in_threadpool(_list_registrations_sync, settings.registry_path)
    payload = [
        {
            "collection": reg.collection,
            "directory": reg.directory,
            "registered_at": reg.registered_at,
        }
        for reg in regs
    ]
    return JSONResponse({"total_registrations": len(payload), "registrations": payload})


def _server_home() -> tuple[Path | None, str | None]:
    """Return the server process's home directory from the passwd database.

    Uses ``pwd.getpwuid(os.getuid())`` rather than ``$HOME`` so that a remote
    client cannot widen the allowlist by influencing the server's environment.
    Returns ``(None, reason)`` if the passwd entry cannot be resolved.
    """
    try:
        entry = pwd.getpwuid(os.getuid())
    except KeyError as exc:
        return None, f"cannot determine server home directory: {exc}"
    try:
        return Path(entry.pw_dir).resolve(), None
    except (OSError, RuntimeError) as exc:
        return None, f"cannot resolve server home directory: {exc}"


def _resolve_registration_path(directory: str) -> tuple[Path | None, str | None]:
    """Return the resolved absolute path, or an error reason.

    Rejects anything that resolves outside the server process's home
    directory (as reported by the passwd database, not ``$HOME``).  A
    remote client must not be able to register ``/etc`` or ``/root/.ssh``
    and then siphon their contents out via subsequent sync.
    """
    if ".." in Path(directory).parts:
        return None, "directory must not contain '..'"
    try:
        resolved = Path(directory).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"cannot resolve directory: {exc}"

    home, reason = _server_home()
    if home is None:
        return None, reason
    try:
        resolved.relative_to(home)
    except ValueError:
        return None, f"directory {str(resolved)!r} is outside {str(home)!r}"
    return resolved, None


def _register_sync(
    registry_path: Path, resolved: Path, collection: str
) -> DirectoryRegistration:
    """Open registry, register, close — all in the caller's thread.

    SQLite connections are bound to the thread that created them, so the
    open/use/close lifecycle must stay inside the worker thread handling
    the request.
    """
    conn = SyncRegistry(registry_path)
    try:
        return conn.register_directory(resolved, collection)
    finally:
        conn.close()


async def _handle_add_registration(request: Request) -> JSONResponse:
    """Register a directory as an async 202 background task."""
    size_err = RequestGuards.check_body_size(request, MAX_REGISTRATIONS_BODY_BYTES)
    if size_err is not None:
        return size_err

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    directory = body.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        return JSONResponse(
            {"error": "Missing required field: directory"}, status_code=400
        )
    collection = body.get("collection")
    if not isinstance(collection, str) or not collection.strip():
        return JSONResponse(
            {"error": "Missing required field: collection"}, status_code=400
        )

    resolved, reason = _resolve_registration_path(directory)
    if resolved is None:
        return JSONResponse({"error": reason}, status_code=400)
    if not resolved.is_dir():
        return JSONResponse(
            {"error": f"directory not found: {resolved}"}, status_code=400
        )

    ctx = _ctx(request)
    state = _begin_task(ctx, "register")
    task = asyncio.create_task(_run_register_task(ctx, state, resolved, collection))
    task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
    ctx.task_refs[state.task_id] = task

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted"},
        status_code=202,
    )


async def _run_register_task(
    ctx: _QuarryContext,
    state: TaskState,
    resolved: Path,
    collection: str,
) -> None:
    """Execute register_directory in background and update task state."""
    try:
        reg = await run_in_threadpool(
            _register_sync, ctx.settings.registry_path, resolved, collection
        )
        state.status = "completed"
        state.results = {
            "directory": reg.directory,
            "collection": reg.collection,
            "registered_at": reg.registered_at,
        }
    except asyncio.CancelledError:
        state.status = "failed"
        state.error = "task was cancelled"
        raise
    except FileNotFoundError as exc:
        state.status = "failed"
        state.error = str(exc)
    except ValueError as exc:
        state.status = "failed"
        state.error = str(exc)
    except Exception as exc:
        logger.exception("Background register failed")
        state.status = "failed"
        state.error = str(exc)
    finally:
        if state.status == "running":
            state.status = "failed"
            state.error = "task exited without setting terminal status"


def _deregister_sync(registry_path: Path, collection: str) -> tuple[bool, list[str]]:
    """Open registry, deregister, close — all in one thread."""
    conn = SyncRegistry(registry_path)
    try:
        existing = conn.get_registration(collection)
        if existing is None:
            return False, []
        removed_docs = conn.deregister_directory(collection)
        return True, removed_docs
    finally:
        conn.close()


async def _handle_delete_registration(request: Request) -> JSONResponse:
    """Deregister synchronously (existence + registry row); purge chunks async."""
    collection = request.query_params.get("collection", "")
    if not collection:
        return JSONResponse(
            {"error": "Missing required parameter: collection"}, status_code=400
        )
    keep_data_raw = request.query_params.get("keep_data", "false").lower()
    if keep_data_raw not in {"true", "false"}:
        return JSONResponse(
            {"error": "keep_data must be 'true' or 'false'"}, status_code=400
        )
    keep_data = keep_data_raw == "true"

    ctx = _ctx(request)
    not_found = JSONResponse(
        {"error": f"No registration found for {collection!r}"}, status_code=404
    )
    if not ctx.settings.registry_path.exists():
        return not_found

    try:  # existence + registry mutation off-thread; unknown -> 404, error -> 500
        found, removed_docs = await run_in_threadpool(
            _deregister_sync, ctx.settings.registry_path, collection
        )
    except Exception as exc:
        logger.exception("Synchronous deregister failed")
        return JSONResponse({"error": f"deregister failed: {exc}"}, status_code=500)
    if not found:
        return not_found

    state = _begin_task(ctx, "deregister")
    state.results = {
        "collection": collection,
        "removed": len(removed_docs),
        "deleted_chunks": 0,
        "type": "registration",
    }
    purge_docs = [] if keep_data else removed_docs
    if purge_docs:
        task = asyncio.create_task(_run_purge_task(ctx, state, collection, purge_docs))
        task.add_done_callback(partial(_on_task_done, ctx, state.task_id))
        ctx.task_refs[state.task_id] = task
    else:
        state.status = "completed"  # nothing to purge; complete immediately

    return JSONResponse(
        {"task_id": state.task_id, "status": "accepted", "removed": len(removed_docs)},
        status_code=202,
    )


async def _run_purge_task(
    ctx: _QuarryContext,
    state: TaskState,
    collection: str,
    removed_docs: list[str],
) -> None:
    """Purge chunks for an already-deregistered collection; update task state."""

    def _purge() -> int:
        store = ctx.database.store
        return sum(
            store.delete_document(d, collection=collection) for d in removed_docs
        )

    with _task_terminal(state):
        state.results["deleted_chunks"] = await run_in_threadpool(_purge)
        state.status = "completed"


def _status_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    ctx = _ctx(request)
    settings = ctx.settings
    chunks = ctx.database.store.count()
    cols = ctx.database.catalog.list_collections()
    doc_count = sum(c["document_count"] for c in cols)

    if settings.registry_path.exists():
        conn = SyncRegistry(settings.registry_path)
        try:
            regs = conn.list_registrations()
        finally:
            conn.close()
    else:
        regs = []

    db_size_bytes = (
        dir_size_bytes(settings.lancedb_path) if settings.lancedb_path.exists() else 0
    )

    return JSONResponse(
        {
            "document_count": doc_count,
            "collection_count": len(cols),
            "chunk_count": chunks,
            "registered_directories": len(regs),
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
            "provider": ProviderSelection.display_cached(),
            "embedding_dimension": settings.embedding_dimension,
        }
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(
    ctx: _QuarryContext,
    *,
    lifespan: Callable[[Starlette], AbstractAsyncContextManager[None]] | None = None,
) -> Starlette:
    """Build the Starlette ASGI application.

    Exposed as a factory so tests can construct the app without starting
    uvicorn — just wrap with ``starlette.testclient.TestClient``.
    """
    origins = list(ctx.cors_origins)

    routes = [
        Route("/health", _health_route, methods=["GET"]),
        Route("/ca.crt", _ca_cert_route, methods=["GET"]),
        Route("/search", _search_route, methods=["GET"]),
        Route("/show", _show_route, methods=["GET"]),
        Route("/documents", _documents_route, methods=["GET"]),
        Route("/documents", _documents_delete_route, methods=["DELETE"]),
        Route("/collections", _collections_route, methods=["GET"]),
        Route("/collections", _collections_delete_route, methods=["DELETE"]),
        Route("/remember", _remember_route, methods=["POST"]),
        Route("/ingest", _ingest_route, methods=["POST"]),
        Route("/sync", _sync_route, methods=["POST"]),
        Route("/captures/push", _captures_push_route, methods=["POST"]),
        # Unified task status endpoint with backwards-compatible aliases.
        Route("/tasks/{task_id}", _task_status_route, methods=["GET"]),
        Route("/sync/{task_id}", _task_status_route, methods=["GET"]),
        Route("/ingest/{task_id}", _task_status_route, methods=["GET"]),
        Route("/databases", _databases_route, methods=["GET"]),
        Route("/use", _use_route, methods=["POST"]),
        Route(
            "/registrations",
            _registrations_route,
            methods=["GET", "POST", "DELETE"],
        ),
        Route("/status", _status_route, methods=["GET"]),
    ]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        ),
    ]

    from starlette.exceptions import HTTPException  # noqa: PLC0415

    async def _json_http_error(
        request: Request,  # noqa: ARG001
        exc: HTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            {"error": exc.detail or "Error"},
            status_code=exc.status_code,
        )

    async def _json_server_error(
        request: Request,
        exc: Exception,  # noqa: ARG001
    ) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"error": "Internal server error"}, status_code=500)

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
        exception_handlers={
            HTTPException: _json_http_error,  # type: ignore[dict-item]
            Exception: _json_server_error,
        },
    )
    app.state.ctx = ctx
    return app


# ---------------------------------------------------------------------------
# Port file helpers
# ---------------------------------------------------------------------------


def _write_port_file(port_path: Path, port: int) -> None:
    port_path.parent.mkdir(parents=True, exist_ok=True)
    port_path.write_text(str(port))
    logger.info("Wrote port file: %s (port %d)", port_path, port)


def _remove_port_file(port_path: Path) -> None:
    try:
        port_path.unlink(missing_ok=True)
        logger.info("Removed port file: %s", port_path)
    except OSError:
        logger.warning("Could not remove port file: %s", port_path)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

# Sample the daemon's open-fd count every 5 minutes so a climbing trend — the
# LanceDB deleted-index-handle leak — surfaces in logs before it reaches EMFILE.
_FD_TELEMETRY_INTERVAL_SECONDS = 300.0


def _validate_host_key(host: str, api_key: str | None) -> None:
    """Refuse to bind to non-loopback without an API key."""
    if host != "127.0.0.1" and not api_key:
        msg = (
            "Refusing to bind to %s without --api-key. "
            "Non-loopback hosts require authentication."
        )
        raise SystemExit(msg % host)


def serve(
    settings: Settings,
    port: int = DEFAULT_PORT,
    *,
    host: str = "127.0.0.1",
    api_key: str | None = None,
    cors_origins: frozenset[str] | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    """Start the HTTP server.  Blocks until shutdown signal.

    Args:
        settings: Resolved application settings.
        port: Port to bind.  Defaults to :data:`DEFAULT_PORT`.
        host: Address to bind.  ``127.0.0.1`` for local-only (default),
            ``0.0.0.0`` for container/production deployment.
        api_key: Optional Bearer token.  When set, all endpoints except
            ``/health`` and ``/ca.crt`` require
            ``Authorization: Bearer <key>``.
        cors_origins: Allowed CORS origins.  Defaults to ``http://localhost``.
        ssl_certfile: Path to TLS server certificate PEM.  When provided
            (with ssl_keyfile), the server uses HTTPS/WSS.
        ssl_keyfile: Path to TLS server private key PEM.
    """
    _validate_host_key(host, api_key)

    port_path = settings.lancedb_path.parent / "serve.port"

    ctx = _QuarryContext(settings, api_key=api_key, cors_origins=cors_origins)
    ctx.warm()  # Build cached resources single-threaded before serving (DES-032).

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        # Runs for the daemon's lifetime; cancelled on shutdown below.
        monitor = asyncio.create_task(FdTelemetry(_FD_TELEMETRY_INTERVAL_SECONDS).run())
        try:
            yield
        finally:
            # Shutdown. Remove the port file first so cleanup is guaranteed
            # even if draining the monitor task surfaces an error.
            monitor.cancel()
            _remove_port_file(port_path)
            with suppress(asyncio.CancelledError):
                await monitor

    app = build_app(ctx, lifespan=lifespan)

    tls_enabled = ssl_certfile is not None and ssl_keyfile is not None
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        log_level="warning",
        access_log=False,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    server = uvicorn.Server(config)

    # Write the port file after bind so callers always see the actual port
    # (important when port == 0 requests an OS-assigned ephemeral port).
    original_startup = server.startup
    scheme = "https" if tls_enabled else "http"

    async def _startup_with_port_file(
        sockets: list[socket] | None = None,
    ) -> None:
        await original_startup(sockets=sockets)
        if server.servers and server.servers[0].sockets:
            actual_port = server.servers[0].sockets[0].getsockname()[1]
            _write_port_file(port_path, actual_port)
            logger.info(
                "Quarry server listening on %s://%s:%d", scheme, host, actual_port
            )
        else:
            logger.error("Server started but no bound sockets; port file not written")

    server.startup = _startup_with_port_file  # type: ignore[method-assign]

    logger.info("Starting Quarry server on %s:%d", host, port)
    server.run()
    logger.info("Server stopped")
