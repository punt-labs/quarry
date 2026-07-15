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

from quarry.daemon.tasks import TaskState, task_terminal

if TYPE_CHECKING:
    from starlette.requests import Request

    from quarry.daemon.context import DaemonContext


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
