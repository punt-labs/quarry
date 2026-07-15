"""The daemon ASGI app factory: wire the route groups onto a Starlette app."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from quarry.daemon.routes import (
    CaptureRoutes,
    CollectionRoutes,
    DatabaseRoutes,
    DocumentRoutes,
    IngestionRoutes,
    MetaRoutes,
    RegistrationRoutes,
    SearchRoutes,
    ShowRoutes,
    SyncRoutes,
    TaskStatusRoutes,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from starlette.requests import Request

    from quarry.daemon.context import DaemonContext

logger = logging.getLogger(__name__)

type Lifespan = Callable[[Starlette], AbstractAsyncContextManager[None]]


@final
class AppBuilder:
    """Assemble the daemon's Starlette application around one context."""

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    def build(self, *, lifespan: Lifespan | None = None) -> Starlette:
        """Return the ASGI app; wrap with ``TestClient`` to exercise routes."""
        app = Starlette(
            routes=self._routes(),
            middleware=self._middleware(),
            lifespan=lifespan,
            exception_handlers={
                HTTPException: self._http_error,  # type: ignore[dict-item]
                Exception: self._server_error,
            },
        )
        app.state.ctx = self._ctx
        return app

    def _routes(self) -> list[Route]:
        ctx = self._ctx
        meta = MetaRoutes(ctx)
        docs = DocumentRoutes(ctx)
        cols = CollectionRoutes(ctx)
        ingestion = IngestionRoutes(ctx)
        tasks = TaskStatusRoutes(ctx)
        db = DatabaseRoutes(ctx)
        return [
            Route("/health", meta.health, methods=["GET"]),
            Route("/ca.crt", meta.ca_cert, methods=["GET"]),
            Route("/search", SearchRoutes(ctx).search, methods=["GET"]),
            Route("/show", ShowRoutes(ctx).show, methods=["GET"]),
            Route("/documents", docs.documents, methods=["GET"]),
            Route("/documents", docs.delete, methods=["DELETE"]),
            Route("/collections", cols.collections, methods=["GET"]),
            Route("/collections", cols.delete, methods=["DELETE"]),
            Route("/remember", ingestion.remember, methods=["POST"]),
            Route("/ingest", ingestion.ingest, methods=["POST"]),
            Route("/sync", SyncRoutes(ctx).sync, methods=["POST"]),
            Route("/captures/push", CaptureRoutes(ctx).push, methods=["POST"]),
            # Unified task status endpoint with backwards-compatible aliases.
            Route("/tasks/{task_id}", tasks.status, methods=["GET"]),
            Route("/sync/{task_id}", tasks.status, methods=["GET"]),
            Route("/ingest/{task_id}", tasks.status, methods=["GET"]),
            Route("/databases", db.databases, methods=["GET"]),
            Route("/use", db.use, methods=["POST"]),
            Route(
                "/registrations",
                RegistrationRoutes(ctx).registrations,
                methods=["GET", "POST", "DELETE"],
            ),
            Route("/status", meta.status, methods=["GET"]),
        ]

    def _middleware(self) -> list[Middleware]:
        return [
            Middleware(
                CORSMiddleware,
                allow_origins=list(self._ctx.cors_origins),
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            ),
        ]

    @staticmethod
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:  # noqa: ARG004
        return JSONResponse(
            {"error": exc.detail or "Error"},
            status_code=exc.status_code,
        )

    @staticmethod
    async def _server_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG004
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


def build_app(ctx: DaemonContext, *, lifespan: Lifespan | None = None) -> Starlette:
    """Build the daemon ASGI application for *ctx* (thin ``AppBuilder`` entry)."""
    return AppBuilder(ctx).build(lifespan=lifespan)
