"""The daemon's REST route table: the ``RouteSpec`` value object and the wiring.

Separated from the app factory so the *what* (the ordered list of routes, their
handlers, and the ``api`` models that document each) lives apart from the *how*
(``AppBuilder`` turning specs into a FastAPI app).  Engine routes are marked
``versioned`` and gain the ``/v1`` prefix at registration; ``/health`` and
``/ca.crt`` are unversioned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, final

from quarry.api import (
    CapturesPushResponse,
    CollectionList,
    DatabaseList,
    DeregisterAccepted,
    DocumentList,
    HealthResponse,
    IngestRequest,
    OptimizeRequest,
    RegisterRequest,
    RegistrationList,
    RememberRequest,
    SearchResponse,
    ShowPageResponse,
    StatusResponse,
    TaskAccepted,
    TaskStatus,
)
from quarry.api.maintenance import BackfillRequest
from quarry.daemon.routes import (
    CaptureRoutes,
    CollectionRoutes,
    DatabaseRoutes,
    DocumentRoutes,
    IngestionRoutes,
    MaintenanceRoutes,
    MetaRoutes,
    RegistrationRoutes,
    SearchRoutes,
    ShowRoutes,
    SyncRoutes,
    TaskStatusRoutes,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from quarry.daemon.context import DaemonContext

# FastAPI types its own endpoints as ``Callable[..., Any]``; our handlers are a
# mix of sync/async bound methods that read the raw ``Request``, so this boundary
# alias is the honest type for "any daemon request handler".
type Endpoint = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """One REST route: its path, handler, and the models that document it."""

    path: str
    endpoint: Endpoint
    methods: tuple[str, ...]
    response_model: type[BaseModel] | None = None
    request_model: type[BaseModel] | None = None
    exclude_none: bool = False
    status_code: int = 200
    versioned: bool = True

    def full_path(self, prefix: str) -> str:
        """Return the route path, prefixing engine routes with the API version."""
        return f"{prefix}{self.path}" if self.versioned else self.path

    def openapi_extra(self) -> dict[str, Any] | None:
        """Return the OpenAPI ``requestBody`` for a POST body, else ``None``.

        The handlers parse the body themselves, so FastAPI is never handed the
        request model to validate; inlining the schema here documents the body
        shape without letting FastAPI's parsing displace the manual contract.
        """
        if self.request_model is None:
            return None
        return {
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": self.request_model.model_json_schema()
                    }
                },
            }
        }


@final
class RouteTable:
    """Build the daemon's ordered REST route specs from one context."""

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    def specs(self) -> tuple[RouteSpec, ...]:
        """Return every route the daemon serves, in registration order."""
        ctx = self._ctx
        meta = MetaRoutes(ctx)
        docs = DocumentRoutes(ctx)
        cols = CollectionRoutes(ctx)
        ingestion = IngestionRoutes(ctx)
        tasks = TaskStatusRoutes(ctx)
        db = DatabaseRoutes(ctx)
        reg = RegistrationRoutes(ctx)
        maint = MaintenanceRoutes(ctx)
        return (
            RouteSpec(
                "/health", meta.health, ("GET",), HealthResponse, versioned=False
            ),
            RouteSpec("/ca.crt", meta.ca_cert, ("GET",), versioned=False),
            RouteSpec("/search", SearchRoutes(ctx).search, ("GET",), SearchResponse),
            RouteSpec("/show", ShowRoutes(ctx).show, ("GET",), ShowPageResponse),
            RouteSpec("/documents", docs.documents, ("GET",), DocumentList),
            RouteSpec(
                "/documents", docs.delete, ("DELETE",), TaskAccepted, status_code=202
            ),
            RouteSpec("/collections", cols.collections, ("GET",), CollectionList),
            RouteSpec(
                "/collections", cols.delete, ("DELETE",), TaskAccepted, status_code=202
            ),
            RouteSpec(
                "/remember",
                ingestion.remember,
                ("POST",),
                TaskAccepted,
                request_model=RememberRequest,
                status_code=202,
            ),
            RouteSpec(
                "/ingest",
                ingestion.ingest,
                ("POST",),
                TaskAccepted,
                request_model=IngestRequest,
                status_code=202,
            ),
            RouteSpec(
                "/sync", SyncRoutes(ctx).sync, ("POST",), TaskAccepted, status_code=202
            ),
            RouteSpec(
                "/captures/push",
                CaptureRoutes(ctx).push,
                ("POST",),
                CapturesPushResponse,
            ),
            RouteSpec(
                "/tasks/{task_id}",
                tasks.status,
                ("GET",),
                TaskStatus,
                exclude_none=True,
            ),
            RouteSpec("/databases", db.databases, ("GET",), DatabaseList),
            RouteSpec("/use", db.use, ("POST",)),
            RouteSpec("/registrations", reg.registrations, ("GET",), RegistrationList),
            RouteSpec(
                "/registrations",
                reg.registrations,
                ("POST",),
                TaskAccepted,
                request_model=RegisterRequest,
                status_code=202,
            ),
            RouteSpec(
                "/registrations",
                reg.registrations,
                ("DELETE",),
                DeregisterAccepted,
                status_code=202,
            ),
            RouteSpec("/status", meta.status, ("GET",), StatusResponse),
            RouteSpec(
                "/optimize",
                maint.optimize,
                ("POST",),
                TaskAccepted,
                request_model=OptimizeRequest,
                status_code=202,
            ),
            RouteSpec(
                "/backfill-sessions",
                maint.backfill_sessions,
                ("POST",),
                TaskAccepted,
                request_model=BackfillRequest,
                status_code=202,
            ),
        )
