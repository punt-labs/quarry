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
import ipaddress
import logging
import os
import pwd
import re
import socket as socket_module
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import cached_property
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
from starlette.routing import Route, WebSocketRoute

from quarry.backends import get_embedding_backend
from quarry.config import DEFAULT_PORT, Settings
from quarry.database import (
    count_chunks,
    delete_collection as db_delete_collection,
    delete_document as db_delete_document,
    format_size,
    get_db,
    get_page_text,
    hybrid_search,
    list_collections as db_list_collections,
    list_documents,
)
from quarry.provider import provider_display
from quarry.sync_registry import (
    DirectoryRegistration,
    deregister_directory,
    get_registration,
    list_registrations,
    open_registry,
    register_directory,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from starlette.requests import Request
    from starlette.websockets import WebSocket

    from quarry.types import EmbeddingBackend, LanceDB

logger = logging.getLogger(__name__)

_DEFAULT_CORS_ORIGINS = frozenset({"http://localhost"})

_AUTH_EXEMPT_PATHS = frozenset({"/health", "/ca.crt"})

# Strip control characters from user-supplied log values (CWE-117).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

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
    # urlsplit lowercases the scheme, but be defensive.
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


def _coerce_bool_field(
    body: dict[str, object], field: str, *, default: bool
) -> bool | JSONResponse:
    """Return the bool value of ``body[field]`` or a 400 response.

    Rejects any non-bool non-null value.  Python's ``bool()`` coerces the
    strings ``"false"`` and ``"0"`` to ``True`` — use this helper instead to
    preserve caller intent.
    """
    value = body.get(field)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return JSONResponse(
        {"error": f"Field {field!r} must be a boolean"},
        status_code=400,
    )


def _pipeline_error_response(exc: Exception, label: str) -> JSONResponse:
    """Map an ingest/sync pipeline exception to a JSON error response.

    The split is intentional:

    * ``ValueError`` means the caller sent something the pipeline could not
      parse — return 400 so the client can fix its input.
    * ``OSError`` means a downstream transport failure (disk, network);
      502 signals the issue is upstream of the client.
    * Anything else is logged with the full traceback and wrapped in a 500
      JSON envelope so the CLI never sees an HTML error page.
    """
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, OSError):
        return JSONResponse({"error": str(exc)}, status_code=502)
    logger.exception("%s pipeline failure", label)
    return JSONResponse(
        {"error": f"{label} failed: {exc}"},
        status_code=500,
    )


def _check_body_size(request: Request, limit: int) -> JSONResponse | None:
    """Reject requests whose advertised body size exceeds *limit*.

    Also rejects chunked-encoding requests with no ``Content-Length`` header
    so the server cannot be forced to stream arbitrary bytes before noticing.
    """
    header = request.headers.get("content-length")
    if header is None:
        return JSONResponse(
            {"error": "Content-Length header required"},
            status_code=411,
        )
    try:
        length = int(header)
    except ValueError:
        return JSONResponse({"error": "Invalid Content-Length header"}, status_code=400)
    if length < 0:
        return JSONResponse({"error": "Invalid Content-Length header"}, status_code=400)
    if length > limit:
        return JSONResponse(
            {"error": f"Request body too large (max {limit} bytes)"},
            status_code=413,
        )
    return None


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

    limit_str = request.query_params.get("limit", "10")
    try:
        limit = max(1, min(int(limit_str), 50))
    except ValueError:
        limit = 10

    collection = request.query_params.get("collection") or None
    page_type = request.query_params.get("page_type") or None
    source_format = request.query_params.get("source_format") or None
    document = request.query_params.get("document") or None
    agent_handle = request.query_params.get("agent_handle") or None
    memory_type = request.query_params.get("memory_type") or None

    ctx = _ctx(request)
    query_vector = ctx.embedder.embed_query(query)
    results = hybrid_search(
        ctx.db,
        query,
        query_vector,
        limit=limit,
        document_filter=document,
        collection_filter=collection,
        page_type_filter=page_type,
        source_format_filter=source_format,
        agent_handle_filter=agent_handle,
        memory_type_filter=memory_type,
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
            "agent_handle": r.get("agent_handle"),
            "memory_type": r.get("memory_type"),
            "summary": r.get("summary", ""),
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

    if request.method == "DELETE":
        return _handle_delete_document(request)

    collection = request.query_params.get("collection") or None
    ctx = _ctx(request)
    docs = list_documents(ctx.db, collection_filter=collection)
    return JSONResponse({"total_documents": len(docs), "documents": docs})


def _collections_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    if request.method == "DELETE":
        return _handle_delete_collection(request)

    ctx = _ctx(request)
    cols = db_list_collections(ctx.db)
    return JSONResponse({"total_collections": len(cols), "collections": cols})


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
        text = get_page_text(ctx.db, document, page, collection=collection)
        if text is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(
            {"document_name": document, "page_number": page, "text": text}
        )

    # No page or page == 0: return document metadata.
    docs = list_documents(ctx.db, collection_filter=collection)
    match = [d for d in docs if d["document_name"] == document]
    if not match:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(match[0])


async def _remember_route(request: Request) -> JSONResponse:
    """Ingest inline text content. Body: {name, content, ...optional}."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    size_err = _check_body_size(request, MAX_REMEMBER_BODY_BYTES)
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
    overwrite = _coerce_bool_field(body, "overwrite", default=True)
    if isinstance(overwrite, JSONResponse):
        return overwrite
    agent_handle = body.get("agent_handle") or ""
    memory_type = body.get("memory_type") or ""
    summary = body.get("summary") or ""

    from quarry.pipeline import ingest_content  # noqa: PLC0415

    ctx = _ctx(request)
    # ingest_content is synchronous and performs embedding + DB writes.
    # Run it in the threadpool so the event loop stays responsive.
    try:
        result = await run_in_threadpool(
            ingest_content,
            content,
            name,
            ctx.db,
            ctx.settings,
            overwrite=overwrite,
            collection=str(collection),
            format_hint=str(format_hint),
            agent_handle=str(agent_handle),
            memory_type=str(memory_type),
            summary=str(summary),
        )
    except Exception as exc:  # noqa: BLE001 — routed through _pipeline_error_response
        return _pipeline_error_response(exc, "remember")
    return JSONResponse(dict(result))


async def _ingest_route(request: Request) -> JSONResponse:
    """Ingest a URL. Body: {source, ...optional}. File upload not supported."""
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    size_err = _check_body_size(request, MAX_INGEST_BODY_BYTES)
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

    overwrite = _coerce_bool_field(body, "overwrite", default=False)
    if isinstance(overwrite, JSONResponse):
        return overwrite
    collection = body.get("collection") or ""
    agent_handle = body.get("agent_handle") or ""
    memory_type = body.get("memory_type") or ""
    summary = body.get("summary") or ""

    from quarry.pipeline import ingest_auto  # noqa: PLC0415

    ctx = _ctx(request)
    # ingest_auto fetches the URL, embeds pages, and writes to LanceDB —
    # all synchronous blocking work.  Offload to the threadpool.
    try:
        result = await run_in_threadpool(
            ingest_auto,
            source,
            ctx.db,
            ctx.settings,
            overwrite=overwrite,
            collection=str(collection),
            agent_handle=str(agent_handle),
            memory_type=str(memory_type),
            summary=str(summary),
        )
    except Exception as exc:  # noqa: BLE001 — routed through _pipeline_error_response
        return _pipeline_error_response(exc, "ingest")
    return JSONResponse(dict(result))


async def _sync_route(request: Request) -> JSONResponse:
    """Run ``sync_all`` against the registered directories.  Body is ignored.

    The local ``sync_all`` function has no per-collection filter: it walks
    every registration in the registry and returns a ``{collection: result}``
    mapping.  The endpoint mirrors that shape.
    """
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    size_err = _check_body_size(request, MAX_SYNC_BODY_BYTES)
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

    from quarry.sync import sync_all  # noqa: PLC0415

    ctx = _ctx(request)
    try:
        results = await run_in_threadpool(sync_all, ctx.db, ctx.settings)
    except Exception as exc:  # noqa: BLE001 — routed through _pipeline_error_response
        return _pipeline_error_response(exc, "sync")

    return JSONResponse(
        {
            collection: {
                "ingested": res.ingested,
                "deleted": res.deleted,
                "skipped": res.skipped,
                "failed": res.failed,
                "errors": list(res.errors),
            }
            for collection, res in results.items()
        }
    )


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
            docs = list_documents(ctx.db)
        except Exception:  # noqa: BLE001 — table may not exist yet
            logger.debug("list_documents failed on fresh database", exc_info=True)
            docs = []
    else:
        docs = []
    size_bytes = (
        sum(f.stat().st_size for f in lance_dir.rglob("*") if f.is_file())
        if lance_dir.exists()
        else 0
    )
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
    conn = open_registry(registry_path)
    try:
        return list_registrations(conn)
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
    conn = open_registry(registry_path)
    try:
        return register_directory(conn, resolved, collection)
    finally:
        conn.close()


async def _handle_add_registration(request: Request) -> JSONResponse:
    size_err = _check_body_size(request, MAX_REGISTRATIONS_BODY_BYTES)
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
    try:
        reg = await run_in_threadpool(
            _register_sync, ctx.settings.registry_path, resolved, collection
        )
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(
        {
            "directory": reg.directory,
            "collection": reg.collection,
            "registered_at": reg.registered_at,
        }
    )


def _deregister_sync(registry_path: Path, collection: str) -> tuple[bool, list[str]]:
    """Open registry, deregister, close — all in one thread."""
    conn = open_registry(registry_path)
    try:
        existing = get_registration(conn, collection)
        if existing is None:
            return False, []
        removed_docs = deregister_directory(conn, collection)
        return True, removed_docs
    finally:
        conn.close()


async def _handle_delete_registration(request: Request) -> JSONResponse:
    collection = request.query_params.get("collection", "")
    if not collection:
        return JSONResponse(
            {"error": "Missing required parameter: collection"}, status_code=400
        )

    keep_data_raw = request.query_params.get("keep_data", "false").lower()
    if keep_data_raw not in {"true", "false"}:
        return JSONResponse(
            {"error": "keep_data must be 'true' or 'false'"},
            status_code=400,
        )
    keep_data = keep_data_raw == "true"

    ctx = _ctx(request)
    if not ctx.settings.registry_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)

    found, removed_docs = await run_in_threadpool(
        _deregister_sync, ctx.settings.registry_path, collection
    )
    if not found:
        return JSONResponse({"error": "Not found"}, status_code=404)

    deleted_chunks = 0
    if not keep_data and removed_docs:

        def _purge() -> int:
            total = 0
            for doc_name in removed_docs:
                total += db_delete_document(ctx.db, doc_name, collection=collection)
            return total

        deleted_chunks = await run_in_threadpool(_purge)

    return JSONResponse(
        {
            "collection": collection,
            "removed": len(removed_docs),
            "deleted_chunks": deleted_chunks,
            "type": "registration",
        }
    )


def _handle_delete_document(request: Request) -> JSONResponse:
    name = request.query_params.get("name", "")
    if not name:
        return JSONResponse(
            {"error": "Missing required parameter: name"}, status_code=400
        )

    collection = request.query_params.get("collection") or None
    ctx = _ctx(request)
    count = db_delete_document(ctx.db, name, collection=collection)
    if count == 0:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"deleted": count, "name": name, "type": "document"})


def _handle_delete_collection(request: Request) -> JSONResponse:
    name = request.query_params.get("name", "")
    if not name:
        return JSONResponse(
            {"error": "Missing required parameter: name"}, status_code=400
        )

    ctx = _ctx(request)
    count = db_delete_collection(ctx.db, name)
    if count == 0:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"deleted": count, "name": name, "type": "collection"})


def _status_route(request: Request) -> JSONResponse:
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp

    ctx = _ctx(request)
    settings = ctx.settings
    docs = list_documents(ctx.db)
    chunks = count_chunks(ctx.db)
    cols = db_list_collections(ctx.db)

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

    return JSONResponse(
        {
            "document_count": len(docs),
            "collection_count": len(cols),
            "chunk_count": chunks,
            "registered_directories": len(regs),
            "database_path": str(settings.lancedb_path),
            "database_size_bytes": db_size_bytes,
            "embedding_model": settings.embedding_model,
            "provider": provider_display(),
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
        Route("/ca.crt", _ca_cert_route, methods=["GET"]),
        Route("/search", _search_route, methods=["GET"]),
        Route("/show", _show_route, methods=["GET"]),
        Route("/documents", _documents_route, methods=["GET", "DELETE"]),
        Route("/collections", _collections_route, methods=["GET", "DELETE"]),
        Route("/remember", _remember_route, methods=["POST"]),
        Route("/ingest", _ingest_route, methods=["POST"]),
        Route("/sync", _sync_route, methods=["POST"]),
        Route("/databases", _databases_route, methods=["GET"]),
        Route("/use", _use_route, methods=["POST"]),
        Route(
            "/registrations",
            _registrations_route,
            methods=["GET", "POST", "DELETE"],
        ),
        Route("/status", _status_route, methods=["GET"]),
        WebSocketRoute("/mcp", _mcp_websocket_route),
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
    """Start the HTTP + WebSocket server.  Blocks until shutdown signal.

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
