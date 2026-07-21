"""The content-ingestion routes: inline-text (remember) and URL (ingest).

Each request is validated into an immutable *job* value object that owns its own
background execution, so the validated fields travel together instead of as a
ten-argument task function.
"""

from __future__ import annotations

from typing import final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.captures_collection import CapturesCollection
from quarry.daemon.ingest_jobs import IngestJob, ScrubbedIngestJob
from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.url_safety import UrlSafetyCheck
from quarry.http_guards import RequestGuards

# Maximum request body sizes.  Remember accepts content, ingest only a URL.
MAX_REMEMBER_BODY_BYTES = 50 * 1024 * 1024
MAX_INGEST_BODY_BYTES = 1 * 1024 * 1024


@final
class IngestionRoutes(RouteGroup):
    """Serve inline-text and URL ingestion as 202 background tasks."""

    async def remember(self, request: Request) -> JSONResponse:
        """Ingest inline text content as a background task.

        Body: {name, content, ...optional}. Returns 202 Accepted with a task_id.
        """
        body = await self._authorized_body(request, MAX_REMEMBER_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        job = self._remember_job(body)
        if isinstance(job, JSONResponse):
            return job

        state = self.ctx.tasks.begin("remember")
        return self.submit(job.collection, job, state)

    async def ingest(self, request: Request) -> JSONResponse:
        """Ingest a URL as a background task.

        Body: {source, ...optional}. File upload not supported.
        Returns 202 Accepted immediately with a task_id; the actual ingest
        runs as an asyncio background task, polled by that task id.  Unlike
        sync, multiple concurrent ingests are allowed.
        """
        body = await self._authorized_body(request, MAX_INGEST_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body

        source = self._require_text(body, "source")
        if isinstance(source, JSONResponse):
            return source
        # UrlSafetyCheck owns all scheme + address validation.  It calls
        # getaddrinfo(), which can block on DNS — run it in the threadpool.
        reason = await run_in_threadpool(UrlSafetyCheck.reject_reason, source)
        if reason is not None:
            return JSONResponse({"error": f"URL rejected: {reason}"}, status_code=400)

        job = await self._ingest_job(body, source)
        if isinstance(job, JSONResponse):
            return job

        state = self.ctx.tasks.begin("ingest")
        return self.submit(job.collection, job, state)

    def _remember_job(
        self, body: dict[str, object]
    ) -> ScrubbedIngestJob | JSONResponse:
        """Validate a remember body into a :class:`ScrubbedIngestJob` or a 400."""
        name = self._require_text(body, "name")
        if isinstance(name, JSONResponse):
            return name
        content = self._require_text(body, "content")
        if isinstance(content, JSONResponse):
            return content
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=True)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        return ScrubbedIngestJob(
            name=name,
            content=content,
            collection=self._str_field(body, "collection", "default"),
            format_hint=self._str_field(body, "format_hint", "auto"),
            overwrite=overwrite,
            scrub_label="remember",
            agent_handle=self._str_field(body, "agent_handle", ""),
            memory_type=self._str_field(body, "memory_type", ""),
            summary=self._str_field(body, "summary", ""),
        )

    async def _ingest_job(
        self, body: dict[str, object], source: str
    ) -> IngestJob | JSONResponse:
        """Validate an ingest body into an :class:`IngestJob` or a 400."""
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=False)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        scrub = RequestGuards.coerce_bool_field(body, "scrub", default=False)
        if isinstance(scrub, JSONResponse):
            return scrub
        collection = await self._ingest_collection(body, scrub=scrub)
        return IngestJob(
            source=source,
            overwrite=overwrite,
            collection=collection,
            scrub=scrub,
            agent_handle=self._str_field(body, "agent_handle", ""),
            memory_type=self._str_field(body, "memory_type", ""),
            summary=self._str_field(body, "summary", ""),
        )

    async def _ingest_collection(self, body: dict[str, object], *, scrub: bool) -> str:
        """Resolve the collection the queue keys on: captures for a web-fetch.

        A web-fetch capture (``scrub``) writes the ``<repo>-captures`` collection
        derived from ``cwd`` — resolved here, before the job is built, so the
        queue can route it.  ``for_registry_path`` reads the registry, so it runs
        off the event loop.  A plain ingest keeps the body's collection (the
        pipeline derives a hostname when it is empty).
        """
        if scrub:
            captures = await run_in_threadpool(
                CapturesCollection.for_registry_path,
                self._str_field(body, "cwd", ""),
                self.ctx.settings.registry_path,
            )
            return captures.name
        return self._str_field(body, "collection", "")
