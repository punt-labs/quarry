"""Lightweight HTTP server exposing Quarry search over localhost.

Designed for the quarry-menubar macOS companion app. Uses the standard
library ``http.server`` with zero additional dependencies — the single
client and fast operations (embed + search < 100ms) don't require async.

Lifecycle:
    1. ``quarry serve`` loads settings + embedding model (cold start)
    2. Writes port to ``~/.quarry/data/<db>/serve.port``
    3. Serves JSON endpoints on ``localhost:<port>``
    4. Cleans up port file on SIGTERM/SIGINT
"""

from __future__ import annotations

import hmac
import json
import logging
import signal
import threading
import time
from functools import cached_property
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from quarry.backends import get_embedding_backend
from quarry.config import Settings
from quarry.database import (
    count_chunks,
    get_db,
    list_collections as db_list_collections,
    list_documents,
    search,
)

if TYPE_CHECKING:
    from quarry.types import EmbeddingBackend, LanceDB

logger = logging.getLogger(__name__)

_DEFAULT_CORS_ORIGINS = frozenset({"http://localhost"})


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

    @cached_property
    def db(self) -> LanceDB:
        return get_db(self._settings.lancedb_path)

    @cached_property
    def embedder(self) -> EmbeddingBackend:
        return get_embedding_backend(self._settings)

    @property
    def settings(self) -> Settings:
        return self._settings


class QuarryHTTPHandler(BaseHTTPRequestHandler):
    """Handle GET requests for Quarry search, documents, collections, status."""

    server: QuarryHTTPServer  # pyright: ignore[reportIncompatibleVariableOverride]

    _AUTH_EXEMPT_PATHS = frozenset({"/health"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if not self._check_auth(parsed.path):
            return

        routes: dict[str, Any] = {
            "/health": self._handle_health,
            "/search": self._handle_search,
            "/documents": self._handle_documents,
            "/collections": self._handle_collections,
            "/status": self._handle_status,
        }

        handler = routes.get(parsed.path)
        if handler is None:
            self._send_json({"error": "Not found"}, status=404)
            return

        try:
            handler(params)
        except Exception:
            logger.exception("Error handling %s", urlparse(self.path).path)
            self._send_json({"error": "Internal server error"}, status=500)

    def _check_auth(self, path: str) -> bool:
        """Verify Bearer token if api_key is configured. Returns True if ok."""
        api_key = self.server.ctx.api_key
        if not api_key or path in self._AUTH_EXEMPT_PATHS:
            return True

        auth_header = self.headers.get("Authorization", "")
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            self._send_json({"error": "Unauthorized"}, status=401)
            return False

        if not hmac.compare_digest(parts[1], api_key):
            self._send_json({"error": "Unauthorized"}, status=401)
            return False

        return True

    def _cors_headers(self) -> dict[str, str]:
        """Build CORS headers, reflecting the Origin if it's in the allow list.

        Always emits ``Vary: Origin`` so caches key on origin regardless of
        match — prevents a non-CORS cached response from being served to a
        valid CORS request.
        """
        origin = self.headers.get("Origin", "")
        headers: dict[str, str] = {"Vary": "Origin"}
        if origin in self.server.ctx.cors_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        return headers

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Redact query strings from access logs to avoid leaking user input
        # (CWE-532). BaseHTTPRequestHandler may include the full request line
        # (e.g. "GET /search?q=secret HTTP/1.1") among these positional args,
        # so we defensively redact query strings from all string arguments.
        redacted_args = tuple(
            self._redact_query_string(a) if isinstance(a, str) else a for a in args
        )
        logger.info(format, *redacted_args)

    @staticmethod
    def _redact_query_string(value: str) -> str:
        """Strip query parameters from request line strings.

        Preserves HTTP version in request lines (e.g. "GET /path HTTP/1.1").
        """
        if "?" not in value:
            return value

        # Handle HTTP request line: "<METHOD> <TARGET> HTTP/<VERSION>"
        parts = value.split(None, 2)
        if len(parts) == 3 and parts[2].startswith("HTTP/"):
            method, target, version = parts
            return f"{method} {target.split('?', 1)[0]} {version}"

        # Fallback: drop everything after the first "?"
        return value.split("?", 1)[0]

    def _ctx(self) -> _QuarryContext:
        return self.server.ctx

    def _send_json(self, data: object, *, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _param(self, params: dict[str, list[str]], key: str, default: str = "") -> str:
        values = params.get(key, [])
        return values[0] if values else default

    def _handle_health(self, _params: dict[str, list[str]]) -> None:
        self._send_json(
            {
                "status": "ok",
                "uptime_seconds": round(time.monotonic() - self.server.start_time, 1),
            }
        )

    def _handle_search(self, params: dict[str, list[str]]) -> None:
        query = self._param(params, "q")
        if not query:
            self._send_json({"error": "Missing required parameter: q"}, status=400)
            return

        limit_str = self._param(params, "limit", "10")
        try:
            limit = max(1, min(int(limit_str), 50))
        except ValueError:
            limit = 10

        collection = self._param(params, "collection") or None
        page_type = self._param(params, "page_type") or None
        source_format = self._param(params, "source_format") or None

        ctx = self._ctx()
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
        self._send_json(
            {
                "query": query,
                "total_results": len(formatted),
                "results": formatted,
            }
        )

    def _handle_documents(self, params: dict[str, list[str]]) -> None:
        collection = self._param(params, "collection") or None
        ctx = self._ctx()
        docs = list_documents(ctx.db, collection_filter=collection)
        self._send_json({"total_documents": len(docs), "documents": docs})

    def _handle_collections(self, _params: dict[str, list[str]]) -> None:
        ctx = self._ctx()
        cols = db_list_collections(ctx.db)
        self._send_json({"total_collections": len(cols), "collections": cols})

    def _handle_status(self, _params: dict[str, list[str]]) -> None:
        ctx = self._ctx()
        settings = ctx.settings
        docs = list_documents(ctx.db)
        chunks = count_chunks(ctx.db)
        cols = db_list_collections(ctx.db)

        db_size_bytes = (
            sum(
                f.stat().st_size
                for f in settings.lancedb_path.rglob("*")
                if f.is_file()
            )
            if settings.lancedb_path.exists()
            else 0
        )

        self._send_json(
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


class QuarryHTTPServer(HTTPServer):
    """HTTPServer subclass carrying shared Quarry context."""

    def __init__(self, address: tuple[str, int], ctx: _QuarryContext) -> None:
        self.ctx = ctx
        self.start_time = time.monotonic()
        super().__init__(address, QuarryHTTPHandler)


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


def serve(
    settings: Settings,
    port: int = 0,
    *,
    api_key: str | None = None,
    cors_origins: frozenset[str] | None = None,
) -> None:
    """Start the HTTP server. Blocks until shutdown signal.

    Args:
        settings: Resolved application settings.
        port: Port to bind (0 = OS-assigned).
        api_key: Optional Bearer token. When set, all GET endpoints except
            /health require ``Authorization: Bearer <key>``.
        cors_origins: Allowed CORS origins. Defaults to ``http://localhost``.
    """
    port_path = settings.lancedb_path.parent / "serve.port"

    ctx = _QuarryContext(settings, api_key=api_key, cors_origins=cors_origins)
    # Eagerly load embedding model so cold-start happens before serving
    logger.info("Loading embedding model...")
    _ = ctx.embedder
    logger.info("Embedding model ready")

    server = QuarryHTTPServer(("127.0.0.1", port), ctx)
    actual_port = server.server_address[1]

    try:
        _write_port_file(port_path, actual_port)
    except Exception:
        logger.exception("Failed to write port file: %s", port_path)
        server.server_close()
        raise

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down", signum)
        _remove_port_file(port_path)
        # Run shutdown in a thread to avoid deadlocking serve_forever()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Quarry HTTP server listening on http://127.0.0.1:%d", actual_port)
    try:
        server.serve_forever()
    finally:
        _remove_port_file(port_path)
        server.server_close()
        logger.info("Server stopped")
