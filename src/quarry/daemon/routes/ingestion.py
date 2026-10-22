"""The content-ingestion routes: inline-text (remember) and URL (ingest).

Each request is validated into an immutable *job* value object that owns its own
background execution, so the validated fields travel together instead of as a
ten-argument task function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.captures_collection import CapturesCollection
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.routes.content_group import ContentRouteGroup
from quarry.http_guards import RequestGuards
from quarry.ingest_collection import IngestCollection
from quarry.lesson import LessonComposer, LessonsCollection

if TYPE_CHECKING:
    from quarry.http_url import HttpUrl

# Maximum request body sizes.  Remember accepts content, ingest only a URL.
MAX_REMEMBER_BODY_BYTES = 50 * 1024 * 1024
MAX_INGEST_BODY_BYTES = 1 * 1024 * 1024
MAX_LEARN_BODY_BYTES = 64 * 1024


@final
class IngestionRoutes(ContentRouteGroup):
    """Serve inline-text and URL ingestion as 202 background tasks."""

    async def remember(self, request: Request) -> JSONResponse:
        """Ingest inline text content as a background task.

        Body: {name, content, ...optional}. Returns 202 Accepted with a task_id.
        """
        body = await self._authorized_body(request, MAX_REMEMBER_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        return self._accept("remember", self._remember_job(body))

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
        oversized = self._reject_oversized(body, "ingest")
        if oversized is not None:
            return oversized

        source = self._require_text(body, "source")
        if isinstance(source, JSONResponse):
            return source
        url = await self._admit_url(source)
        if isinstance(url, JSONResponse):
            return url
        return self._accept("ingest", await self._ingest_job(body, url))

    async def learn(self, request: Request) -> JSONResponse:
        """Save a distilled lesson as a background task.

        remember = a specific durable fact, ingest = a URL, learn = a
        distilled lesson that gets retrieval preference. Body: {lesson,
        topic?, name?, cwd?}. Always writes memory_type="lesson" with no
        agent_handle -- a lesson is project-scoped, deliberately-curated
        knowledge, never a personal, decaying memory (fusion.py's decay gate
        exempts empty-handle rows already). Returns 202 with a task_id.
        """
        body = await self._authorized_body(request, MAX_LEARN_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        return self._accept("learn", await self._learn_job(body))

    def _remember_job(
        self, body: dict[str, object]
    ) -> ScrubbedIngestJob | JSONResponse:
        """Validate a remember body into a :class:`ScrubbedIngestJob` or a 400.

        Collection routing is a single server-side rule so no surface can
        drift: an empty collection with an ``agent_handle`` lands
        in ``memory-<handle>``; empty on both sides falls back to ``default``;
        an explicit collection always wins.
        """
        name = self._require_text(body, "name")
        if isinstance(name, JSONResponse):
            return name
        content = self._require_content(body, "content", "remember")
        if isinstance(content, JSONResponse):
            return content
        options = self._require_options(body)
        if isinstance(options, JSONResponse):
            return options
        overwrite, memory_type, format_hint = options
        # ``memory-<handle>`` is longer than the handle that passed the label cap.
        collection = self._resolve_memory_collection(
            self._str_field(body, "collection", ""),
            self._str_field(body, "agent_handle", ""),
        )
        refused = self._reject_oversized({"collection": collection}, "remember")
        if refused is not None:
            return refused
        labels = self._labels(
            memory_type, body, overwrite=overwrite, collection=collection
        )
        return ScrubbedIngestJob(name, content, labels, format_hint, "remember")

    async def _learn_job(
        self, body: dict[str, object]
    ) -> ScrubbedIngestJob | JSONResponse:
        """Validate a learn body into a :class:`ScrubbedIngestJob` or a 400.

        Naming, collection routing, and memory_type are single server-side
        rules so no surface can drift them.
        """
        lesson = self._require_text(body, "lesson")
        if isinstance(lesson, JSONResponse):
            return lesson
        rejection = self._reject_oversized(body, "learn") or self._reject_binary(
            lesson, "learn"
        )
        if rejection is not None:
            return rejection
        try:
            LessonComposer.check_length(lesson)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        topic = self._str_field(body, "topic", "")
        name = self._str_field(body, "name", "")
        collection = await run_in_threadpool(
            LessonsCollection.for_registry_path,
            self._str_field(body, "cwd", ""),
            self.ctx.settings.registry_path,
        )
        labels = self._labels(
            self._narrow(LessonComposer.memory_type()),
            {"summary": topic},
            overwrite=False,
            collection=collection.name,
        )
        return ScrubbedIngestJob(
            LessonComposer.document_name(name, topic), lesson, labels, "auto", "learn"
        )

    @staticmethod
    def _resolve_memory_collection(raw_collection: str, agent_handle: str) -> str:
        """Return the effective collection for a remember write."""
        if raw_collection:
            return raw_collection
        if agent_handle:
            return f"memory-{agent_handle}"
        return "default"

    async def _ingest_job(
        self, body: dict[str, object], source: HttpUrl
    ) -> IngestJob | JSONResponse:
        """Validate an ingest body into an :class:`IngestJob` or a 400."""
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=False)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        scrub = RequestGuards.coerce_bool_field(body, "scrub", default=False)
        if isinstance(scrub, JSONResponse):
            return scrub
        memory_type = self._require_memory_type(body)
        if isinstance(memory_type, JSONResponse):
            return memory_type
        collection = (
            await self._captures_collection(body)
            if scrub
            else self._str_field(body, "collection", "")
        )
        # Key the queue on the ACTUAL table: the explicit/captures name if set,
        # else the URL hostname — the SAME resolver the pipeline applies, so the
        # queue key never diverges from the table the job writes (single writer).
        collection = IngestCollection.resolve(source, collection).name
        labels = self._labels(
            memory_type, body, overwrite=overwrite, collection=collection
        )
        return IngestJob(source, labels, scrub)

    async def _captures_collection(self, body: dict[str, object]) -> str:
        """Return the ``<repo>-captures`` collection derived from ``body['cwd']``.

        A web-fetch capture (``scrub``) writes there, resolved off the event loop
        because ``for_registry_path`` reads the registry.
        """
        captures = await run_in_threadpool(
            CapturesCollection.for_registry_path,
            self._str_field(body, "cwd", ""),
            self.ctx.settings.registry_path,
        )
        return captures.name
