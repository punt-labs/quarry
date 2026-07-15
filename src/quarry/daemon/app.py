"""The daemon ASGI app factory: wire the route table onto a FastAPI app.

Engine routes live under the ``/v1`` prefix; ``/health`` and ``/ca.crt`` stay
unversioned so a client can probe liveness and bootstrap trust before it knows
the wire version.  Each route carries its ``api`` response model (and, for POST
bodies, the request schema) purely for the published OpenAPI document — the
handlers keep parsing the wire by hand so every clamp, coercion, and error shape
stays byte-for-byte what the Starlette handlers returned (bug-class-3).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.api import API_VERSION
from quarry.daemon.route_table import RouteTable
from quarry.http_guards import RequestGuards

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from starlette.types import ASGIApp, Receive, Scope, Send

    from quarry.daemon.context import DaemonContext

logger = logging.getLogger(__name__)

# Derived from the one wire version so daemon and clients never disagree on it.
_API_PREFIX = f"/v{API_VERSION}"

type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


@final
class JsonContentTypeGuard:
    """ASGI middleware: every ``POST`` must carry ``Content-Type: application/json``.

    A fail-closed CSRF choke point for the no-auth loopback daemon.  A browser
    can send the CORS "simple" content types (``text/plain``, the form types) or
    none at all cross-origin without a preflight; requiring JSON forces a
    preflight the daemon's CORS policy refuses.  Because it sits at request
    admission it guards *every* POST — including routes that read no body
    (``/captures/push``) or accept an empty one (optimize/backfill/sync) and any
    future POST route — so no handler has to repeat the check.  ``GET``/``DELETE``
    /``OPTIONS`` pass through: none is a CORS-simple state-changing request.
    """

    __slots__ = ("_app",)

    _app: ASGIApp

    def __new__(cls, app: ASGIApp) -> Self:
        self = super().__new__(cls)
        self._app = app
        return self

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject a non-JSON POST with 415; otherwise defer to the wrapped app.

        Only request headers are read (never the body), so ``receive`` is passed
        through untouched and the downstream handler still parses the body.
        """
        if scope["type"] == "http" and scope["method"] == "POST":
            guard = RequestGuards.require_json_content_type(Request(scope, receive))
            if guard is not None:
                await guard(scope, receive, send)
                return
        await self._app(scope, receive, send)


@final
class AppBuilder:
    """Assemble the daemon's FastAPI application around one context."""

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    def build(self, *, lifespan: Lifespan | None = None) -> FastAPI:
        """Return the ASGI app; wrap with ``TestClient`` to exercise routes.

        The interactive docs and ``/openapi.json`` HTTP routes are disabled so
        the served surface is exactly the daemon's own routes; ``app.openapi()``
        still renders the schema in-process for ``make openapi``.
        """
        app = FastAPI(
            title="Quarry daemon",
            version=API_VERSION,
            lifespan=lifespan,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        self._add_middleware(app)
        self._register_routes(app)
        app.add_exception_handler(RequestValidationError, self._validation_error)
        app.add_exception_handler(HTTPException, self._http_error)
        app.add_exception_handler(Exception, self._server_error)
        app.state.ctx = self._ctx
        return app

    def _add_middleware(self, app: FastAPI) -> None:
        """Attach the JSON-content-type CSRF guard, then the CORS middleware.

        ``add_middleware`` wraps outermost-last, so CORS ends up outside the
        guard and still answers preflight ``OPTIONS`` requests; the guard only
        acts on ``POST`` and passes everything else through.
        """
        app.add_middleware(JsonContentTypeGuard)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(self._ctx.cors_origins),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    def _register_routes(self, app: FastAPI) -> None:
        """Register every route table entry, versioning engine routes with /v1."""
        for spec in RouteTable(self._ctx).specs():
            app.add_api_route(
                spec.full_path(_API_PREFIX),
                spec.endpoint,
                methods=list(spec.methods),
                response_model=spec.response_model,
                response_model_exclude_none=spec.exclude_none,
                status_code=spec.status_code,
                response_class=spec.response_class,
                openapi_extra=spec.openapi_extra(),
            )

    @staticmethod
    async def _validation_error(_request: Request, _exc: Exception) -> JSONResponse:
        """Render FastAPI's 422 body validation into the ``{"error": ...}`` shape."""
        return JSONResponse({"error": "Invalid request body"}, status_code=422)

    @staticmethod
    async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
        detail = exc.detail if isinstance(exc, HTTPException) else "Error"
        status = exc.status_code if isinstance(exc, HTTPException) else 500
        return JSONResponse({"error": detail or "Error"}, status_code=status)

    @staticmethod
    async def _server_error(request: Request, _exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


def build_app(ctx: DaemonContext, *, lifespan: Lifespan | None = None) -> FastAPI:
    """Build the daemon ASGI application for *ctx* (thin ``AppBuilder`` entry)."""
    return AppBuilder(ctx).build(lifespan=lifespan)
