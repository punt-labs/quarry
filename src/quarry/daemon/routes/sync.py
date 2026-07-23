"""The sync route: enqueue a scan per registered collection onto the watch loop."""

from __future__ import annotations

from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.http_guards import RequestGuards

# The sync body carries only small option dicts.
MAX_SYNC_BODY_BYTES = 16 * 1024


@final
class SyncRoutes(RouteGroup):
    """Serve the sync request — enqueue collection scans, always 202 (DES-045)."""

    async def sync(self, request: Request) -> JSONResponse:
        """Accept a sync request and enqueue a scan per registered collection.

        DES-045 drops the DES-026 409: with the watch loop always active a 409
        would reject every explicit sync the moment the daemon indexes anything.
        The request instead enqueues ``CollectionSyncJob``(s) behind the live
        watch work and returns 202 + task_id — the same poll-to-completion shape
        the CLI already uses.  The ``reject_if_running`` 409 stays on
        optimize/backfill, which are genuine singletons with no per-collection
        queue.  The 202 body and ``/v1/tasks`` schema are byte-identical to
        before; only the failure mode (409 → transparent enqueue) changed.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_SYNC_BODY_BYTES)
        if size_err is not None:
            return size_err

        # Reject malformed JSON so clients do not think the server silently
        # ignored their arguments.  An empty body is fine.
        if int(request.headers.get("content-length", "0") or "0") > 0:
            body = await self.json_object(request)
            if isinstance(body, JSONResponse):
                return body

        state = self.ctx.tasks.begin("sync")
        return self.accept(state, self.ctx.watch_loop.request_scan(state))
