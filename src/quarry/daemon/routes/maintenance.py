"""The maintenance routes: optimize (compact) and backfill-sessions.

Both run engine work off the event loop as tracked 202 background tasks, so a
long compaction or transcript scan never blocks the daemon.  The backfill's four
validated options travel together as an immutable :class:`BackfillArgs` rather
than as a four-argument task function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.tasks import TaskState, task_terminal
from quarry.http_guards import RequestGuards

# Both bodies carry only small option dicts.
MAX_MAINTENANCE_BODY_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class BackfillArgs:
    """The validated backfill options, bundled as one value."""

    dry_run: bool
    collection: str
    project: str
    limit: int


@final
class MaintenanceRoutes(RouteGroup):
    """Serve table optimization and session backfill as 202 tasks."""

    async def optimize(self, request: Request) -> JSONResponse:
        """Compact the table and rebuild indexes as a background task."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_MAINTENANCE_BODY_BYTES)
        if size_err is not None:
            return size_err

        force = await self._body_flag(request, "force", default=False)
        if isinstance(force, JSONResponse):
            return force

        state = self.ctx.tasks.begin("optimize")
        return self.accept(state, self._run_optimize(state, force=force))

    async def backfill_sessions(self, request: Request) -> JSONResponse:
        """Scan and ingest historical session transcripts as a background task."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_MAINTENANCE_BODY_BYTES)
        if size_err is not None:
            return size_err

        args = await self._backfill_args(request)
        if isinstance(args, JSONResponse):
            return args

        state = self.ctx.tasks.begin("backfill")
        return self.accept(state, self._run_backfill(state, args))

    async def _run_optimize(self, state: TaskState, *, force: bool) -> None:
        """Count fragments, optimize, and record the before-count in *state*."""
        with task_terminal(state):
            opt = self.ctx.database.optimizer
            fragments = await run_in_threadpool(opt.count_fragments)
            await run_in_threadpool(lambda: opt.optimize(force=force))
            state.status = "completed"
            state.results = {
                "optimized": True,
                "fragments_before": fragments,
                "force": force,
            }

    async def _run_backfill(self, state: TaskState, args: BackfillArgs) -> None:
        """Run the transcript backfill in a worker thread and record its stats."""
        from quarry.backfill import backfill_sessions  # noqa: PLC0415

        with task_terminal(state):
            stats = await run_in_threadpool(
                backfill_sessions,
                self.ctx.settings,
                dry_run=args.dry_run,
                collection_override=args.collection,
                project_filter=args.project,
                limit=args.limit,
            )
            state.status = "completed"
            state.results = {
                "ingested": stats.ingested,
                "skipped_existing": stats.skipped_existing,
                "skipped_unregistered": stats.skipped_unregistered,
                "skipped_empty": stats.skipped_empty,
                "errors": list(stats.errors),
                "dry_run": args.dry_run,
            }

    async def _body_flag(
        self, request: Request, key: str, *, default: bool
    ) -> bool | JSONResponse:
        """Parse an optional JSON body and coerce ``body[key]`` to a bool.

        An empty body is fine — the flag falls back to *default*; a malformed
        body or a non-bool ``key`` is a 400, matching the sync contract.
        """
        if int(request.headers.get("content-length", "0") or "0") <= 0:
            return default
        body = await self.json_object(request)
        if isinstance(body, JSONResponse):
            return body
        return RequestGuards.coerce_bool_field(body, key, default=default)

    async def _backfill_args(self, request: Request) -> BackfillArgs | JSONResponse:
        """Validate a backfill request body into :class:`BackfillArgs`."""
        if int(request.headers.get("content-length", "0") or "0") <= 0:
            return BackfillArgs(dry_run=False, collection="", project="", limit=0)
        body = await self.json_object(request)
        if isinstance(body, JSONResponse):
            return body

        dry_run = RequestGuards.coerce_bool_field(body, "dry_run", default=False)
        if isinstance(dry_run, JSONResponse):
            return dry_run
        limit = RequestGuards.coerce_int_field(body, "limit", default=0)
        if isinstance(limit, JSONResponse):
            return limit
        return BackfillArgs(
            dry_run=dry_run,
            collection=str(body.get("collection") or ""),
            project=str(body.get("project") or ""),
            limit=limit,
        )
