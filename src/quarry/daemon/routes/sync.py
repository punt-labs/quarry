"""The ``/sync`` route: run ``sync_all`` as a singleton background task."""

from __future__ import annotations

from typing import final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.tasks import TaskState, task_terminal
from quarry.http_guards import RequestGuards

# The /sync body carries only small option dicts.
MAX_SYNC_BODY_BYTES = 16 * 1024


@final
class SyncRoutes(RouteGroup):
    """Serve ``POST /sync`` — reject concurrent syncs with 409, else 202."""

    async def sync(self, request: Request) -> JSONResponse:
        """Accept a sync request and run ``sync_all`` as a background task.

        Uses a non-blocking check to reject concurrent requests with HTTP 409.
        Returns 202 Accepted immediately with a task_id; the actual sync runs
        as an asyncio background task.  ``GET /tasks/<task_id>`` returns the
        task status.
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

        running_sync = self.ctx.tasks.running_of_kind("sync")
        if running_sync is not None:
            return JSONResponse(
                {
                    "error": "Sync already in progress",
                    "status": "running",
                    "task_id": running_sync.task_id,
                },
                status_code=409,
            )

        state = self.ctx.tasks.begin("sync")
        return self.accept(state, self._run_sync(state))

    async def _run_sync(self, state: TaskState) -> None:
        """Execute sync_all in a background thread and update *state*."""
        from quarry.sync import sync_all  # noqa: PLC0415

        with task_terminal(state):
            results = await run_in_threadpool(
                sync_all, self.ctx.database.db, self.ctx.settings
            )
            state.status = "completed"
            state.results = {
                collection: {
                    "ingested": res.ingested,
                    "refreshed": res.refreshed,
                    "deleted": res.deleted,
                    "skipped": res.skipped,
                    "failed": res.failed,
                    "errors": list(res.errors),
                }
                for collection, res in results.items()
            }
