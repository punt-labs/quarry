"""The capture routes: file a scrubbed capture, and push enabled shadows."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.captures_collection import CapturesCollection
from quarry.daemon.ingest_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.url_safety import UrlSafetyCheck
from quarry.http_guards import RequestGuards

if TYPE_CHECKING:
    from quarry.config import Settings

# The push request carries no body of note; cap it like the other option posts.
MAX_CAPTURES_BODY_BYTES = 16 * 1024
# The capture body carries a transcript (~500 KB budget); cap it well above that
# but far below remember's, so a runaway capture cannot exhaust the daemon.
MAX_CAPTURE_BODY_BYTES = 4 * 1024 * 1024


@final
class CaptureRoutes(RouteGroup):
    """Serve capture ingest (``POST /v1/capture``) and capture push."""

    async def capture(self, request: Request) -> JSONResponse:
        """File a scrubbed capture as a 202 background task.

        Body: {content, cwd, ...optional}. The daemon derives the target
        ``<repo>-captures`` collection from ``cwd`` and always scrubs.
        """
        body = await self._authorized_body(request, MAX_CAPTURE_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        job = await self._capture_job(body)
        if isinstance(job, JSONResponse):
            return job
        state = self.ctx.tasks.begin("capture")
        return self.submit(job.collection, job, state)

    async def _capture_job(
        self, body: dict[str, object]
    ) -> CaptureIngestJob | JSONResponse:
        """Validate a capture body into a :class:`CaptureIngestJob` or a 400."""
        content = self._require_text(body, "content")
        if isinstance(content, JSONResponse):
            return content
        name = self._capture_name(body)
        if isinstance(name, JSONResponse):
            return name
        overwrite = RequestGuards.coerce_bool_field(body, "overwrite", default=True)
        if isinstance(overwrite, JSONResponse):
            return overwrite
        source_url = self._str_field(body, "source_url")
        rejection = await self._reject_unsafe_source(source_url)
        if rejection is not None:
            return rejection
        collection = await run_in_threadpool(
            CapturesCollection.for_registry_path,
            self._str_field(body, "cwd"),
            self.ctx.settings.registry_path,
        )
        inline = ScrubbedIngestJob(
            name=name,
            content=content,
            collection=collection.name,
            format_hint=self._str_field(body, "format_hint", "auto"),
            overwrite=overwrite,
            scrub_label="capture",
            agent_handle=self._str_field(body, "agent_handle"),
            memory_type=self._str_field(body, "memory_type"),
            summary=self._str_field(body, "summary"),
        )
        return CaptureIngestJob(inline=inline, source_url=source_url)

    async def _reject_unsafe_source(self, source_url: str) -> JSONResponse | None:
        """Reject a source_url that resolves to a private/metadata address.

        The daemon re-fetches this URL server-side when the inline HTML extracts
        to zero chunks, so it is an SSRF sink identical to ``POST /ingest`` — it
        must run the same UrlSafetyCheck gate at the route boundary, fail-closed,
        before the job is built.  An empty source_url (a transcript) is nothing
        to fetch and needs no gate.  reject_reason calls getaddrinfo, which can
        block on DNS, so run it in the threadpool.
        """
        if not source_url:
            return None
        reason = await run_in_threadpool(UrlSafetyCheck.reject_reason, source_url)
        if reason is not None:
            return JSONResponse({"error": f"URL rejected: {reason}"}, status_code=400)
        return None

    def _capture_name(self, body: dict[str, object]) -> str | JSONResponse:
        """Derive the document name: explicit name, else ``session-<id[:8]>``.

        Inputs are stripped and only accepted when non-empty, so a
        whitespace-only ``document_name``/``session_id`` earns the 400 rather
        than storing a blank-named document.
        """
        document_name = self._str_field(body, "document_name").strip()
        if document_name:
            return document_name
        session_id = self._str_field(body, "session_id").strip()
        if session_id:
            return f"session-{session_id[:8]}"
        return JSONResponse(
            {"error": "Missing document_name or session_id"}, status_code=400
        )

    async def push(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp
        size_err = RequestGuards.check_body_size(request, MAX_CAPTURES_BODY_BYTES)
        if size_err is not None:
            return size_err
        results = await run_in_threadpool(self._push_registered, self.ctx.settings)
        return JSONResponse({"results": results})

    @staticmethod
    def _push_registered(settings: Settings) -> dict[str, dict[str, object]]:
        """Push every enabled project's redacted captures (the CLI path)."""
        from quarry.shadow import CaptureSync  # noqa: PLC0415

        results = CaptureSync.push_registered(settings, fail_open=True)
        return {col: res.to_dict() for col, res in results.items()}
