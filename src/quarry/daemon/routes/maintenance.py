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
    """Serve table optimization and session backfill as singleton 202 tasks."""

    def _preflight(self, request: Request) -> JSONResponse | None:
        """Reject an unauthorized or over-size maintenance request, else ``None``.

        The guard prefix both maintenance routes share: authentication first,
        then the small-body ceiling. ``None`` means the request cleared both and
        the route may parse its body and dispatch.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        return RequestGuards.check_body_size(request, MAX_MAINTENANCE_BODY_BYTES)

    async def optimize(self, request: Request) -> JSONResponse:
        """Compact the table and rebuild indexes as a singleton background task.

        A second optimize while one is in flight is rejected with 409 and the
        running task's id, so concurrent runs never contend for the table lock.
        """
        pre = self._preflight(request)
        if pre is not None:
            return pre

        force = await self._body_flag(request, "force", default=False)
        if isinstance(force, JSONResponse):
            return force

        conflict = self.reject_if_running("optimize", "Optimize")
        if conflict is not None:
            return conflict

        state = self.ctx.tasks.begin("optimize")
        return self.accept(state, self._run_optimize(state, force=force))

    async def backfill_sessions(self, request: Request) -> JSONResponse:
        """Scan and ingest historical transcripts as a singleton background task.

        A second backfill while one is in flight is rejected with 409 and the
        running task's id; concurrent scans would double-ingest the same
        sessions and write duplicate chunks.
        """
        pre = self._preflight(request)
        if pre is not None:
            return pre

        args = await self._backfill_args(request)
        if isinstance(args, JSONResponse):
            return args

        conflict = self.reject_if_running("backfill", "Backfill")
        if conflict is not None:
            return conflict

        state = self.ctx.tasks.begin("backfill")
        return self.accept(state, self._run_backfill(state, args))

    async def _run_optimize(self, state: TaskState, *, force: bool) -> None:
        """Compact as a background task, recording the *actual* outcome.

        Reports ``optimized`` as whatever the optimizer did — a fragment-count
        skip is ``False`` with a ``reason``, never a false success — and reads
        the fragment count from the outcome so the table is scanned only once.
        """
        with task_terminal(state):
            opt = self.ctx.database.optimizer
            outcome = await run_in_threadpool(lambda: opt.optimize(force=force))
            state.status = "completed"
            results: dict[str, object] = {
                "optimized": outcome.optimized,
                "fragments_before": outcome.fragments,
                "force": force,
            }
            if outcome.reason:
                results["reason"] = outcome.reason
            state.results = results

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
        """Validate a backfill request body into :class:`BackfillArgs`.

        ``limit`` is a pure pagination knob: ``0`` (the wire default, and an
        empty body) means "all", a positive value caps the scan.  The remote and
        local paths share this contract — a backfill run is bounded by
        construction (it streams one transcript at a time and never accumulates
        descriptors, proven by ``test_large_backfill_does_not_leak_descriptors``),
        so no magic-number cap stands in for resource safety.
        """
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
            limit=max(0, limit),
        )
