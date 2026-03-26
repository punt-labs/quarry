"""ASGI HTTP + WebSocket server exposing Quarry search and MCP over localhost.

Designed for the quarry-menubar macOS companion app, production deployment,
and MCP-over-WebSocket for mcp-proxy.  Uses Starlette with uvicorn for
async request handling and native WebSocket support.

Lifecycle:
    1. ``quarry serve`` loads settings + embedding model (cold start)
    2. Writes port to ``~/.punt-labs/quarry/data/<db>/serve.port``
    3. Serves JSON endpoints + ``/mcp`` WebSocket on ``localhost:<port>``
    4. Cleans up port file on shutdown
"""

from __future__ import annotations

import hmac
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import cached_property
from pathlib import Path
from socket import socket
from typing import TYPE_CHECKING

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from quarry.backends import get_embedding_backend
from quarry.config import DEFAULT_PORT, Settings
from quarry.database import (
    count_chunks,
    get_db,
    list_collections as db_list_collections,
    list_documents,
    search,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from starlette.requests import Request
    from starlette.websockets import WebSocket

    from quarry.types import EmbeddingBackend, LanceDB

logger = logging.getLogger(__name__)

_DEFAULT_CORS_ORIGINS = frozenset({"http://localhost"})

_AUTH_EXEMPT_PATHS = frozenset({"/health"})

# Strip control characters from user-supplied log values (CWE-117).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class _QuarryContext:
    """Shared state for the HTTP server: settings, database, embeddings."""

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        cors_origins: frozenset[str] | None = None,
    ) -> None:
        self._settings = settings
        self.api_key = api_key
        self.cors_origins = cors_origins or _DEFAULT_CORS_ORIGINS
        self.start_time = time.monotonic()

    @cached_property
    def db(self) -> LanceDB:
        return get_db(self._settings.lancedb_path)

    @cached_property
    def embedder(self) -> EmbeddingBackend:
        return get_embedding_backend(self._settings)

    @property
    def settings(self) -> Settings:
        return self._settings


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


def _search_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "Missing required parameter: q"}, status_code=400)

    limit_str = request.query_params.get("limit", "10")
    try:
        limit = max(1, min(int(limit_str), 50))
    except ValueError:
        limit = 10

    collection = request.query_params.get("collection") or None
    page_type = request.query_params.get("page_type") or None
    source_format = request.query_params.get("source_format") or None

    ctx = _ctx(request)
    query_vector = ctx.embedder.embed_query(query)
    results = search(
        ctx.db,
        query_vector,
        limit=limit,
        collection_filter=collection,
        page_type_filter=page_type,
        source_format_filter=source_format,
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
    docs = list_documents(ctx.db, collection_filter=collection)
    return JSONResponse({"total_documents": len(docs), "documents": docs})


def _collections_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    ctx = _ctx(request)
    cols = db_list_collections(ctx.db)
    return JSONResponse({"total_collections": len(cols), "collections": cols})


def _status_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    ctx = _ctx(request)
    settings = ctx.settings
    docs = list_documents(ctx.db)
    chunks = count_chunks(ctx.db)
    cols = db_list_collections(ctx.db)

    db_size_bytes = (
        sum(f.stat().st_size for f in settings.lancedb_path.rglob("*") if f.is_file())
        if settings.lancedb_path.exists()
        else 0
    )

    return JSONResponse(
        {
            "document_count": len(docs),
            "collection_count": len(cols),
            "chunk_count": chunks,
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
        }
    )


# ---------------------------------------------------------------------------
# WebSocket MCP endpoint
# ---------------------------------------------------------------------------


async def _mcp_websocket_route(websocket: WebSocket) -> None:
    """MCP JSON-RPC over WebSocket for mcp-proxy.

    Each connection gets its own MCP session with isolated database state
    via ContextVar.  Auth is checked before the WebSocket is accepted.
    """
    from mcp.server.websocket import websocket_server  # noqa: PLC0415

    from quarry.mcp_server import run_mcp_session  # noqa: PLC0415

    ctx: _QuarryContext = websocket.app.state.ctx

    # Reject cross-site WebSocket hijacking (CSWSH).  Browsers always send
    # an Origin header on WebSocket upgrades; non-browser clients (mcp-proxy)
    # do not.  If an Origin is present it must match the allowed CORS origins.
    origin = websocket.headers.get("Origin")
    if origin is not None and origin not in ctx.cors_origins:
        await websocket.close(code=1008)
        return

    # Auth before accept — reject unauthenticated connections immediately.
    if ctx.api_key:
        auth_header = websocket.headers.get("Authorization", "")
        if not _check_bearer_auth(ctx.api_key, auth_header):
            await websocket.close(code=1008)
            return

    # Sanitize user-controlled value before logging (CWE-117).
    raw_key = websocket.query_params.get("session_key", "unknown")
    session_key = _CONTROL_CHAR_RE.sub("", raw_key)[:64]
    logger.info("MCP WebSocket connected: session_key=%s", session_key)

    try:
        async with websocket_server(
            websocket.scope, websocket.receive, websocket.send
        ) as (read_stream, write_stream):
            await run_mcp_session(read_stream, write_stream)
    except Exception:
        logger.exception("MCP WebSocket error: session_key=%s", session_key)
    finally:
        logger.info("MCP WebSocket disconnected: session_key=%s", session_key)


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
        Route("/search", _search_route, methods=["GET"]),
        Route("/documents", _documents_route, methods=["GET"]),
        Route("/collections", _collections_route, methods=["GET"]),
        Route("/status", _status_route, methods=["GET"]),
        WebSocketRoute("/mcp", _mcp_websocket_route),
    ]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "OPTIONS"],
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
) -> None:
    """Start the HTTP + WebSocket server.  Blocks until shutdown signal.

    Args:
        settings: Resolved application settings.
        port: Port to bind.  Defaults to :data:`DEFAULT_PORT`.
        host: Address to bind.  ``127.0.0.1`` for local-only (default),
            ``0.0.0.0`` for container/production deployment.
        api_key: Optional Bearer token.  When set, all endpoints except
            /health require ``Authorization: Bearer <key>``.
        cors_origins: Allowed CORS origins.  Defaults to ``http://localhost``.
    """
    _validate_host_key(host, api_key)

    port_path = settings.lancedb_path.parent / "serve.port"

    ctx = _QuarryContext(settings, api_key=api_key, cors_origins=cors_origins)
    # Eagerly initialize cached properties before serving — cached_property
    # is not thread-safe, so all shared state must be resolved single-threaded.
    logger.info("Loading embedding model...")
    _ = ctx.embedder
    _ = ctx.db
    logger.info("Embedding model ready")

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        # Shutdown
        _remove_port_file(port_path)

    app = build_app(ctx, lifespan=lifespan)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Write the port file after bind so callers always see the actual port
    # (important when port == 0 requests an OS-assigned ephemeral port).
    original_startup = server.startup

    async def _startup_with_port_file(
        sockets: list[socket] | None = None,
    ) -> None:
        await original_startup(sockets=sockets)
        if server.servers and server.servers[0].sockets:
            actual_port = server.servers[0].sockets[0].getsockname()[1]
            _write_port_file(port_path, actual_port)
            logger.info("Quarry server listening on http://%s:%d", host, actual_port)
        else:
            logger.error("Server started but no bound sockets; port file not written")

    server.startup = _startup_with_port_file  # type: ignore[method-assign]

    logger.info("Starting Quarry server on %s:%d", host, port)
    server.run()
    logger.info("Server stopped")
