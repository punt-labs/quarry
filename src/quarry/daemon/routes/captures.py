"""The capture routes: file a scrubbed capture, and push enabled shadows."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.capture_url import CaptureUrl
from quarry.captures_collection import CapturesCollection
from quarry.daemon.ingest_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.routes.base import RouteGroup
from quarry.http_guards import RequestGuards
from quarry.url_safety import UrlSafetyCheck

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
        return self.submit(job, state)

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
        memory_type = self._str_field(body, "memory_type")
        rejection = self.reject_reserved_memory_type(memory_type)
        if rejection is not None:
            return rejection
        source_url = self._str_field(body, "source_url")
        url_rejection = await self._reject_unsafe_source(source_url)
        if url_rejection is not None:
            return url_rejection
        collection = await self._collection_for_cwd(self._str_field(body, "cwd"))
        inline = ScrubbedIngestJob(
            name=name,
            content=content,
            collection=collection.name,
            format_hint=self._str_field(body, "format_hint", "auto"),
            overwrite=overwrite,
            scrub_label="capture",
            agent_handle=self._str_field(body, "agent_handle"),
            memory_type=memory_type,
            summary=self._str_field(body, "summary"),
        )
        return CaptureIngestJob(inline=inline, source_url=source_url)

    async def _collection_for_cwd(self, cwd: str) -> CapturesCollection:
        """Derive the ``<repo>-captures`` collection for *cwd* off the event loop.

        Shared by ``capture`` and ``lookup`` so both routes apply the identical
        registry-derived naming rule from one call site.
        """
        return await run_in_threadpool(
            CapturesCollection.for_registry_path, cwd, self.ctx.settings.registry_path
        )

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

    async def lookup(self, request: Request) -> JSONResponse:
        """Answer whether a URL is already indexed under the caller's captures.

        Body: {url, cwd} — POST, not a ``?url=`` query string: a query-string
        token (an API key, a session id) rides the URL itself and can leak into
        proxy/WAF/browser logs even on a loopback hop (CWE-598).  Shares
        ``POST /capture``'s ``cwd``-derived collection contract
        (:meth:`_capture_job`), so this thin client never opens the sync
        registry itself or spells the ``<repo>-captures`` naming rule.  The
        stored ``document_name`` for a WebFetch capture is the URL with
        userinfo/query/fragment stripped (``CaptureUrl.for_web_fetch``), so this
        recomputes the identical normalization before comparing: two URLs
        differing only by query string or fragment collapse to the SAME
        document and match; a trailing-slash difference does NOT normalize and
        will not match.
        """
        body = await self._authorized_body(request, MAX_CAPTURES_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        url = self._require_text(body, "url")
        if isinstance(url, JSONResponse):
            return url
        collection = await self._collection_for_cwd(self._str_field(body, "cwd"))
        document_name = CaptureUrl.for_web_fetch(url)
        matched = await run_in_threadpool(
            self.ctx.database.catalog.document_exists, document_name, collection.name
        )
        return JSONResponse(
            {"matched": matched, "document_name": document_name if matched else None}
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
