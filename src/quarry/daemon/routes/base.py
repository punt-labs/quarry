"""Shared base for daemon route groups: context access, auth, task plumbing.

Every route group binds one :class:`DaemonContext` at build time and reaches the
engine through it, so a handler never pulls shared state out of ``request.app``.
The base also owns the request-plumbing every group repeats: bearer-token
rejection, JSON-object parsing, and the begin/track/202 background-task dance.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Self

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskState, task_terminal
from quarry.http_guards import RequestGuards

if TYPE_CHECKING:
    from starlette.requests import Request

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit


class RouteGroup:
    """Base for a group of REST handlers bound to one daemon context."""

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    @property
    def ctx(self) -> DaemonContext:
        """Return the daemon context this group serves."""
        return self._ctx

    def reject_unauthorized(self, request: Request) -> JSONResponse | None:
        """Return a 401 response if bearer auth is required but not satisfied.

        ``None`` means the request may proceed — either no key is configured or
        the presented token matched.  Absence-of-rejection is the contract, so
        ``None`` is the success signal here, not a missing value.
        """
        api_key = self._ctx.api_key
        if not api_key:
            return None
        parts = request.headers.get("Authorization", "").split()
        ok = (
            len(parts) == 2
            and parts[0].lower() == "bearer"
            and hmac.compare_digest(parts[1], api_key)
        )
        if not ok:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return None

    async def _authorized_body(
        self, request: Request, max_bytes: int
    ) -> dict[str, object] | JSONResponse:
        """Reject on auth failure or oversize body, else return the JSON object.

        The shared front-of-handler preamble every content POST repeats: reject
        an unauthenticated or oversized request before parsing, then return the
        decoded object (or the 400 a non-object body earns).
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, max_bytes)
        if size_err is not None:
            return size_err
        return await self.json_object(request)

    @staticmethod
    def _str_field(body: dict[str, object], key: str, default: str = "") -> str:
        """Return ``body[key]`` as a string, falling back when absent or empty."""
        return str(body.get(key) or default)

    @staticmethod
    def _require_text(body: dict[str, object], key: str) -> str | JSONResponse:
        """Return a non-empty string ``body[key]`` or a 400 naming the field."""
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            return JSONResponse(
                {"error": f"Missing required field: {key}"}, status_code=400
            )
        return value

    def reject_if_running(self, kind: str, label: str) -> JSONResponse | None:
        """Return a 409 if a task of *kind* is already running, else ``None``.

        The singleton guard shared by the sync, optimize, and backfill routes:
        a second request while one is in flight is rejected with the running
        task's id so the client polls the existing task instead of racing a
        duplicate (a concurrent backfill would double-ingest the same sessions).
        ``None`` means no task of that kind is active and the caller may begin.
        """
        running = self._ctx.tasks.running_of_kind(kind)
        if running is None:
            return None
        return JSONResponse(
            {
                "error": f"{label} already in progress",
                "status": "running",
                "task_id": running.task_id,
            },
            status_code=409,
        )

    def submit(self, job: IngestUnit, state: TaskState) -> JSONResponse:
        """Enqueue *job* on its collection's FIFO worker, returning 202 or 503.

        The routing key is ``job.collection`` — the queue serializes ingest per
        collection and bounds embed concurrency (DES-042).  A full queue returns
        ``503`` — retriable, never a silent drop — and drops the task record so
        no orphan lingers in ``queued``.  The message stays generic:
        ``remember``/``ingest`` share this path and have no spooled local
        artifact, so it cannot promise backfill recovery.
        """
        key = RouteKey(self._ctx.database_name, job.collection)
        if not self._ctx.ingest_queue.try_submit(key, job, state):
            self._ctx.tasks.drop(state)
            return JSONResponse(
                {
                    "error": "ingest queue full; request not accepted, retry shortly",
                    "status": "rejected",
                },
                status_code=503,
            )
        return JSONResponse(
            {"task_id": state.task_id, "status": "accepted"},
            status_code=202,
        )

    def accept(self, state: TaskState, coro: Coroutine[Any, Any, None]) -> JSONResponse:
        """Schedule *coro* as a tracked background task, return 202 Accepted.

        ``coro`` is typed with ``Any`` to match ``asyncio.create_task``'s own
        ``Coroutine[Any, Any, T]`` signature.
        """
        task = asyncio.create_task(coro)
        self._ctx.tasks.track(state, task)
        return JSONResponse(
            {"task_id": state.task_id, "status": "accepted"},
            status_code=202,
        )

    @staticmethod
    async def json_object(request: Request) -> dict[str, object] | JSONResponse:
        """Parse a JSON request body, rejecting non-object payloads.

        Returns the decoded mapping (a wire boundary — values are ``object``
        until each field is narrowed by the caller) or a 400 ``JSONResponse``.
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Body must be a JSON object"}, status_code=400
            )
        return body

    @staticmethod
    async def run_delete(
        state: TaskState,
        delete_call: Callable[[], int],
        name: str,
        kind_label: str,
    ) -> None:
        """Run *delete_call* in a worker thread and record the deleted count.

        Shared by document and collection deletion, which differ only in the
        store method invoked and the ``type`` label reported to the client.
        """
        with task_terminal(state):
            count = await run_in_threadpool(delete_call)
            state.status = "completed"
            state.results = {"deleted": count, "name": name, "type": kind_label}
