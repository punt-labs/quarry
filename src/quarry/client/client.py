"""The pure-transport client every CLI data command drives.

``QuarryClient`` marshals ``quarry.api`` request models to the daemon's REST
routes and parses the responses back into ``quarry.api`` models — one method per
route, so the client can only send what a model carries and only read what a
model declares (bug-class-3 param/field parity).  It is library-safe: it raises
typed :class:`~quarry.client.errors.QuarryError` and never imports ``typer``,
calls ``typer.Exit``/``SystemExit``, or prints — all exit and message handling
lives in the command layer.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Self, final

from pydantic import BaseModel, ValidationError

from quarry.api import (
    API_VERSION,
    BackfillRequest,
    CaptureIngestRequest,
    CapturesPushResponse,
    CollectionList,
    DatabaseList,
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    DeregisterAccepted,
    DeregisterRequest,
    DocumentInfo,
    DocumentList,
    HealthResponse,
    IngestRequest,
    OptimizeRequest,
    RegisterRequest,
    RegistrationList,
    RememberRequest,
    SearchRequest,
    SearchResponse,
    ShowPageResponse,
    ShowRequest,
    StatusResponse,
    TaskAccepted,
    TaskStatus,
)
from quarry.client.errors import QuarryConnectionError, QuarryError
from quarry.client.task import TaskOutcome
from quarry.client.transport import HttpxTransport, Response, Transport

if TYPE_CHECKING:
    from quarry.client.config import ClientConfig

_API_PREFIX = f"/v{API_VERSION}"
_POLL_INTERVAL_S = 0.5
_POLL_TIMEOUT_S = 120.0
_MAX_UNREACHABLE_POLLS = 3


@final
class QuarryClient:
    """Authenticated transport to one daemon target: 20 REST operations plus
    ``await_task`` (a client-side poll over ``task_status``)."""

    _transport: Transport

    def __new__(cls, transport: Transport) -> Self:
        self = super().__new__(cls)
        self._transport = transport
        return self

    @classmethod
    def connect(
        cls, config: ClientConfig, *, transport: Transport | None = None
    ) -> Self:
        """Build a client for *config*, or wrap an injected *transport* (tests).

        Production resolves the URL, pinned CA, and live bearer from *config* and
        builds an :class:`HttpxTransport`; tests inject an ``ASGITransport``-backed
        transport over the real daemon app so the fake cannot drift from the wire.
        """
        # Explicit None check, not `or`: an injected transport whose __bool__ is
        # falsy must still be used, never silently replaced by a real HttpxTransport.
        resolved = (
            HttpxTransport.from_mapping(config.remote_mapping())
            if transport is None
            else transport
        )
        return cls(resolved)

    # -- search & show -----------------------------------------------------

    def search(self, req: SearchRequest) -> SearchResponse:
        """Run a hybrid search; every filter travels in the one request model."""
        return self._get("/search", SearchResponse, params=self._query_params(req))

    def show_page(self, req: ShowRequest) -> ShowPageResponse:
        """Return one page's text (``req.page`` must be set)."""
        return self._get("/show", ShowPageResponse, params=self._show_params(req))

    def show_document(self, req: ShowRequest) -> DocumentInfo:
        """Return a document's catalog metadata (``req.page`` omitted)."""
        return self._get("/show", DocumentInfo, params=self._show_params(req))

    # -- documents & collections ------------------------------------------

    def list_documents(self, collection: str) -> DocumentList:
        """List indexed documents, optionally scoped to one collection."""
        params = {"collection": collection} if collection else None
        return self._get("/documents", DocumentList, params=params)

    def delete_document(self, req: DeleteDocumentRequest) -> TaskAccepted:
        """Delete a document as a 202 background task."""
        params = {"name": req.name}
        if req.collection:
            params["collection"] = req.collection
        return self._delete("/documents", TaskAccepted, params=params)

    def list_collections(self) -> CollectionList:
        """List every collection with its document and chunk counts."""
        return self._get("/collections", CollectionList)

    def delete_collection(self, req: DeleteCollectionRequest) -> TaskAccepted:
        """Delete a collection as a 202 background task."""
        return self._delete("/collections", TaskAccepted, params={"name": req.name})

    # -- ingestion ---------------------------------------------------------

    def remember(self, req: RememberRequest) -> TaskAccepted:
        """Index inline text content as a 202 background task."""
        return self._post("/remember", TaskAccepted, req)

    def capture(self, req: CaptureIngestRequest) -> TaskAccepted:
        """File a scrubbed capture (transcript or fetched page) as a 202 task."""
        return self._post("/capture", TaskAccepted, req)

    def ingest_url(self, req: IngestRequest) -> TaskAccepted:
        """Fetch and index a URL as a 202 background task."""
        return self._post("/ingest", TaskAccepted, req)

    # -- sync & captures ---------------------------------------------------

    def sync(self) -> TaskAccepted:
        """Sync every registered directory as a singleton 202 background task."""
        return self._post_empty("/sync", TaskAccepted)

    def captures_push(self) -> CapturesPushResponse:
        """Push each project's redacted capture shadow."""
        return self._post_empty("/captures/push", CapturesPushResponse)

    # -- registrations -----------------------------------------------------

    def list_registrations(self) -> RegistrationList:
        """List every registered directory."""
        return self._get("/registrations", RegistrationList)

    def register(self, req: RegisterRequest) -> TaskAccepted:
        """Register a directory for sync as a 202 background task."""
        return self._post("/registrations", TaskAccepted, req)

    def deregister(self, req: DeregisterRequest) -> DeregisterAccepted:
        """Deregister a collection; the chunk purge runs as a 202 task."""
        params = {"collection": req.collection, "keep_data": str(req.keep_data).lower()}
        return self._delete("/registrations", DeregisterAccepted, params=params)

    # -- databases, status, maintenance, health ----------------------------

    def list_databases(self) -> DatabaseList:
        """List the single database the daemon is fixed to."""
        return self._get("/databases", DatabaseList)

    def status(self) -> StatusResponse:
        """Return the aggregate status over the daemon's database."""
        return self._get("/status", StatusResponse)

    def optimize(self, req: OptimizeRequest) -> TaskAccepted:
        """Compact the table and rebuild indexes as a singleton 202 task."""
        return self._post("/optimize", TaskAccepted, req)

    def backfill_sessions(self, req: BackfillRequest) -> TaskAccepted:
        """Backfill historical session transcripts as a singleton 202 task."""
        return self._post("/backfill-sessions", TaskAccepted, req)

    def health(self) -> HealthResponse:
        """Return the daemon's liveness snapshot (the unversioned route)."""
        resp = self._transport.request("GET", "/health")
        return self._model(resp, HealthResponse)

    # -- async task polling ------------------------------------------------

    def task_status(self, task_id: str) -> TaskStatus:
        """Poll one background task's current state."""
        return self._get(f"/tasks/{task_id}", TaskStatus)

    def await_task(self, task_id: str) -> TaskOutcome:
        """Poll *task_id* to a terminal :class:`TaskOutcome`.

        Never prints, exits, or names an operation.  Consecutive connection
        losses short-circuit to an ``unreachable`` outcome after
        ``_MAX_UNREACHABLE_POLLS`` rather than polling to the deadline; a single
        transient blip resets the counter.
        """
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        unreachable = 0
        while time.monotonic() < deadline:
            try:
                status = self.task_status(task_id)
            except QuarryConnectionError as exc:
                unreachable += 1
                if unreachable >= _MAX_UNREACHABLE_POLLS:
                    return TaskOutcome.unreachable(task_id, str(exc))
                time.sleep(_POLL_INTERVAL_S)
                continue
            unreachable = 0
            if status.status == "completed":
                return TaskOutcome.completed(task_id, status.results or {})
            if status.status == "failed":
                return TaskOutcome.failed(task_id, status.error or "unknown error")
            time.sleep(_POLL_INTERVAL_S)
        return TaskOutcome.timed_out(task_id)

    # -- request helpers ---------------------------------------------------

    def _get[M: BaseModel](
        self, path: str, model: type[M], *, params: Mapping[str, str] | None = None
    ) -> M:
        resp = self._transport.request("GET", f"{_API_PREFIX}{path}", params=params)
        return self._model(resp, model)

    def _delete[M: BaseModel](
        self, path: str, model: type[M], *, params: Mapping[str, str]
    ) -> M:
        resp = self._transport.request("DELETE", f"{_API_PREFIX}{path}", params=params)
        return self._model(resp, model)

    def _post[M: BaseModel](self, path: str, model: type[M], req: BaseModel) -> M:
        resp = self._transport.request(
            "POST", f"{_API_PREFIX}{path}", json_body=req.model_dump()
        )
        return self._model(resp, model)

    def _post_empty[M: BaseModel](self, path: str, model: type[M]) -> M:
        resp = self._transport.request("POST", f"{_API_PREFIX}{path}", json_body={})
        return self._model(resp, model)

    @staticmethod
    def _model[M: BaseModel](resp: Response, model: type[M]) -> M:
        """Validate the response body into *model*, or raise :class:`QuarryError`."""
        body = resp.json_body
        if not isinstance(body, Mapping):
            raise QuarryError(
                f"Malformed response from remote server: expected a JSON object, "
                f"got {type(body).__name__}"
            )
        try:
            return model.model_validate(dict(body))
        except ValidationError as exc:
            raise QuarryError(f"Malformed response from remote server: {exc}") from exc

    @staticmethod
    def _query_params(req: SearchRequest) -> dict[str, str]:
        """Encode a search request, dropping empty filters and aliasing ``q``."""
        dumped = req.model_dump(by_alias=True, exclude_defaults=True)
        return {key: str(value) for key, value in dumped.items()}

    @staticmethod
    def _show_params(req: ShowRequest) -> dict[str, str]:
        """Encode a show request's document, collection, and optional page."""
        params = {"document": req.document}
        if req.collection:
            params["collection"] = req.collection
        if req.page is not None:
            params["page"] = str(req.page)
        return params
