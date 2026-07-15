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

from starlette.responses import JSONResponse, PlainTextResponse

from quarry.api import (
    BackfillRequest,
    CapturesPushResponse,
    CollectionList,
    DatabaseList,
    DeregisterAccepted,
    DocumentInfo,
    DocumentList,
    ErrorBody,
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
    from types import UnionType

    from pydantic import BaseModel
    from starlette.responses import Response

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
    # A single model, or a union (``A | B``) for a route with two 200 shapes —
    # ``/show`` returns page text or document metadata depending on ``page``.
    response_model: type[BaseModel] | UnionType | None = None
    request_model: type[BaseModel] | None = None
    exclude_none: bool = False
    status_code: int = 200
    # The response class FastAPI documents for the route; defaults to JSON but a
    # plain-text route (``/ca.crt``) overrides it so the doc's content type is
    # honest. Runtime is unaffected — handlers return their own Response.
    response_class: type[Response] = JSONResponse
    versioned: bool = True
    # Whether the handler requires a non-empty JSON body; the maintenance
    # endpoints accept an empty body (Content-Length 0), so their documented
    # requestBody must be optional to match what the parser actually accepts.
    body_required: bool = True

    def full_path(self, prefix: str) -> str:
        """Return the route path, prefixing engine routes with the API version."""
        return f"{prefix}{self.path}" if self.versioned else self.path

    def openapi_extra(self) -> dict[str, Any] | None:
        """Return the OpenAPI ``requestBody`` for a POST, else ``None``.

        The handlers parse the body themselves, so FastAPI is never handed the
        request model to validate; inlining the schema here documents the body
        shape without letting FastAPI's parsing displace the manual contract.

        A POST with no request model still advertises an optional
        ``application/json`` body, so the published contract matches the daemon's
        content-type guard, which rejects any non-JSON POST.  GET/DELETE carry
        no body and return ``None``.
        """
        if "POST" not in self.methods:
            return None
        if self.request_model is None:
            schema: dict[str, Any] = {"type": "object"}
            required = False
        else:
            schema = self.request_model.model_json_schema()
            required = self.body_required
        return {
            "requestBody": {
                "required": required,
                "content": {"application/json": {"schema": schema}},
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
            RouteSpec(
                "/ca.crt",
                meta.ca_cert,
                ("GET",),
                versioned=False,
                response_class=PlainTextResponse,
            ),
            RouteSpec("/search", SearchRoutes(ctx).search, ("GET",), SearchResponse),
            # /show returns page text (page>=1) or document metadata (page
            # omitted), so it documents both 200 shapes as a union.
            RouteSpec(
                "/show", ShowRoutes(ctx).show, ("GET",), ShowPageResponse | DocumentInfo
            ),
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
            # /use is the daemon's one always-error route: it rejects client-side
            # db selection with a 400, so the doc advertises 400 + ErrorBody.
            RouteSpec("/use", db.use, ("POST",), ErrorBody, status_code=400),
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
                body_required=False,
            ),
            RouteSpec(
                "/backfill-sessions",
                maint.backfill_sessions,
                ("POST",),
                TaskAccepted,
                request_model=BackfillRequest,
                status_code=202,
                body_required=False,
            ),
        )
