"""The content-ingestion routes: ``/remember`` (inline text) and ``/ingest`` (URL).

Each request is validated into an immutable *job* value object that owns its own
background execution, so the validated fields travel together instead of as a
ten-argument task function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.tasks import TaskState, task_terminal
from quarry.daemon.url_safety import UrlSafetyCheck
from quarry.http_guards import RequestGuards

if TYPE_CHECKING:
    from starlette.requests import Request

    from quarry.daemon.context import DaemonContext

# Maximum request body sizes.  /remember accepts content, /ingest only a URL.
MAX_REMEMBER_BODY_BYTES = 50 * 1024 * 1024
MAX_INGEST_BODY_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RememberJob:
    """A validated ``/remember`` request that indexes inline text content."""

    name: str
    content: str
    collection: str
    format_hint: str
    overwrite: bool
    agent_handle: str
    memory_type: str
    summary: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Execute ingest_content in a background thread and update task state."""
        from quarry.ingestion.pipeline import ingest_content  # noqa: PLC0415

        with task_terminal(state):
            result = await run_in_threadpool(
                ingest_content,
                self.content,
                self.name,
                ctx.database,
                ctx.settings,
                overwrite=self.overwrite,
                collection=self.collection,
                format_hint=self.format_hint,
                agent_handle=self.agent_handle,
                memory_type=self.memory_type,
                summary=self.summary,
            )
            state.status = "completed"
            state.results = dict(result)


@dataclass(frozen=True, slots=True)
class IngestJob:
    """A validated ``/ingest`` request that fetches and indexes a URL."""

    source: str
    overwrite: bool
    collection: str
    agent_handle: str
    memory_type: str
    summary: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Execute ingest_auto in a background thread and update task state."""
        from quarry.ingestion.pipeline import ingest_auto  # noqa: PLC0415

        with task_terminal(state):
            result = await run_in_threadpool(
                ingest_auto,
                self.source,
                ctx.database,
                ctx.settings,
                overwrite=self.overwrite,
                collection=self.collection,
                agent_handle=self.agent_handle,
                memory_type=self.memory_type,
                summary=self.summary,
            )
            state.status = "completed"
            state.results = dict(result)


@final
class IngestionRoutes(RouteGroup):
    """Serve ``POST /remember`` and ``POST /ingest`` as 202 background tasks."""

    async def remember(self, request: Request) -> JSONResponse:
        """Ingest inline text content as a background task.

        Body: {name, content, ...optional}. Returns 202 Accepted with a task_id.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_REMEMBER_BODY_BYTES)
        if size_err is not None:
            return size_err

        body = await self.json_object(request)
        if isinstance(body, JSONResponse):
            return body
        job = self._remember_job(body)
        if isinstance(job, JSONResponse):
            return job

        state = self.ctx.tasks.begin("remember")
        return self.accept(state, job.run(self.ctx, state))

    async def ingest(self, request: Request) -> JSONResponse:
        """Ingest a URL as a background task.

        Body: {source, ...optional}. File upload not supported.
        Returns 202 Accepted immediately with a task_id; the actual ingest
        runs as an asyncio background task.  ``GET /tasks/<task_id>`` returns
        the task status.  Unlike sync, multiple concurrent ingests are allowed.
        """
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_INGEST_BODY_BYTES)
        if size_err is not None:
            return size_err

        body = await self.json_object(request)
        if isinstance(body, JSONResponse):
            return body

        source = body.get("source")
        if not isinstance(source, str) or not source:
            return JSONResponse(
                {"error": "Missing required field: source"}, status_code=400
            )
        # UrlSafetyCheck owns all scheme + address validation.  It calls
        # getaddrinfo(), which can block on DNS — run it in the threadpool.
        reason = await run_in_threadpool(UrlSafetyCheck.reject_reason, source)
        if reason is not None:
            return JSONResponse({"error": f"URL rejected: {reason}"}, status_code=400)

        job = self._ingest_job(body, source)
        if isinstance(job, JSONResponse):
            return job

        state = self.ctx.tasks.begin("ingest")
        return self.accept(state, job.run(self.ctx, state))

    @staticmethod
    def _str_field(body: dict[str, object], key: str, default: str) -> str:
        """Return ``body[key]`` as a string, falling back when absent or empty."""
        return str(body.get(key) or default)

    def _remember_job(self, body: dict[str, object]) -> RememberJob | JSONResponse:
        """Validate a ``/remember`` body into a :class:`RememberJob` or a 400."""
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse(
                {"error": "Missing required field: name"}, status_code=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return JSONResponse(
                {"error": "Missing required field: content"}, status_code=400
            )
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=True)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        return RememberJob(
            name=name,
            content=content,
            collection=self._str_field(body, "collection", "default"),
            format_hint=self._str_field(body, "format_hint", "auto"),
            overwrite=overwrite,
            agent_handle=self._str_field(body, "agent_handle", ""),
            memory_type=self._str_field(body, "memory_type", ""),
            summary=self._str_field(body, "summary", ""),
        )

    def _ingest_job(
        self, body: dict[str, object], source: str
    ) -> IngestJob | JSONResponse:
        """Validate an ``/ingest`` body into an :class:`IngestJob` or a 400."""
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=False)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        return IngestJob(
            source=source,
            overwrite=overwrite,
            collection=self._str_field(body, "collection", ""),
            agent_handle=self._str_field(body, "agent_handle", ""),
            memory_type=self._str_field(body, "memory_type", ""),
            summary=self._str_field(body, "summary", ""),
        )
