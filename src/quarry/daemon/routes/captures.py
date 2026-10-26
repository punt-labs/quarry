"""The capture routes: file a scrubbed capture, and push enabled shadows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.capture_naming import CaptureNaming
from quarry.capture_snapshot import CaptureSnapshot
from quarry.captures_collection import CapturesCollection
from quarry.daemon.content_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.routes.content_group import ContentRouteGroup
from quarry.http_guards import RequestGuards
from quarry.ingestion.legacy_url_names import LegacyUrlNames
from quarry.web_fetch_name import WebFetchName

if TYPE_CHECKING:
    from quarry.config import Settings

# The push request carries no body of note; cap it like the other option posts.
MAX_CAPTURES_BODY_BYTES = 16 * 1024
# The capture body carries a transcript (~500 KB budget); cap it well above that
# but far below remember's, so a runaway capture cannot exhaust the daemon.
MAX_CAPTURE_BODY_BYTES = 4 * 1024 * 1024


@final
class CaptureRoutes(ContentRouteGroup):
    """Serve capture ingest (``POST /v1/capture``) and capture push."""

    async def capture(self, request: Request) -> JSONResponse:
        """File a scrubbed capture as a 202 background task.

        Body: {content, cwd, ...optional}. The daemon derives the target
        ``<repo>-captures`` collection from ``cwd`` and always scrubs.
        """
        body = await self._authorized_body(request, MAX_CAPTURE_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        return self._accept("capture", await self._capture_job(body))

    async def _capture_job(
        self, body: dict[str, object]
    ) -> CaptureIngestJob | JSONResponse:
        """Validate a capture body into a :class:`CaptureIngestJob` or a 400."""
        content = self._require_content(body, "content", "capture")
        if isinstance(content, JSONResponse):
            return content
        identity = self._require_identity(body)
        if isinstance(identity, JSONResponse):
            return identity
        name, snapshot = identity
        options = self._require_options(body)
        if isinstance(options, JSONResponse):
            return options
        overwrite, memory_type, format_hint = options
        source_url = self._str_field(body, "source_url")
        source = await self._admit_url(source_url) if source_url else None
        if isinstance(source, JSONResponse):
            return source
        collection = await self._collection_for_cwd(self._str_field(body, "cwd"))
        # The resolved name carries the ``-captures`` suffix the request did not.
        refused = self._reject_oversized({"collection": collection.name}, "capture")
        if refused is not None:
            return refused
        labels = self._labels(
            memory_type, body, overwrite=overwrite, collection=collection.name
        )
        # A page capture is named for the URL it came from; a transcript, or a
        # capture named for some other URL, is a label and never a page.
        is_page = CaptureNaming.is_page(name, source_url)
        inline = ScrubbedIngestJob(
            name,
            content,
            labels,
            format_hint,
            "capture",
            url_sourced=is_page,
            captured_at=snapshot,
        )
        return CaptureIngestJob(inline=inline, source_url=source if is_page else None)

    @staticmethod
    def _require_strings(body: dict[str, object], keys: tuple[str, ...]) -> None:
        """Raise ``ValueError`` naming the first of *keys* present but not a string."""
        for key in keys:
            value = body.get(key)
            if value is not None and not isinstance(value, str):
                msg = f"{key} must be a string"
                raise ValueError(msg)

    def _require_identity(
        self, body: dict[str, object]
    ) -> tuple[str, datetime | None] | JSONResponse:
        """Return the capture's document name and snapshot time, or a 400.

        The snapshot is ``None`` when ``captured_at`` is absent (queue time).
        """
        try:
            self._require_strings(body, ("session_id", "captured_at"))
            name = CaptureNaming.name(
                self._str_field(body, "document_name"),
                self._str_field(body, "session_id"),
            )
            snapshot = CaptureSnapshot.parse(
                self._str_field(body, "captured_at"), now=datetime.now(UTC)
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return name, snapshot

    async def _collection_for_cwd(self, cwd: str) -> CapturesCollection:
        """Derive the ``<repo>-captures`` collection for *cwd* off the event loop.

        Shared by ``capture`` and ``lookup`` so both routes apply the identical
        registry-derived naming rule from one call site.
        """
        return await run_in_threadpool(
            CapturesCollection.for_registry_path, cwd, self.ctx.settings.registry_path
        )

    async def lookup(self, request: Request) -> JSONResponse:
        """Answer whether a URL is already indexed under the caller's captures.

        Body: {url, cwd} — POST, not a ``?url=`` query string: a query-string
        token (an API key, a session id) rides the URL itself and can leak into
        proxy/WAF/browser logs even on a loopback hop (CWE-598).  Shares
        ``POST /capture``'s ``cwd``-derived collection contract
        (:meth:`_capture_job`), so this thin client never opens the sync
        registry itself or spells the ``<repo>-captures`` naming rule.  The
        stored ``document_name`` for a WebFetch capture is
        ``HttpUrl.document_name`` (``WebFetchName.maybe``), so this recomputes the
        identical normalization before comparing: the fragment and ``;params`` are
        dropped, the query becomes a digest of its sorted parameters, the host is
        lower-cased with its trailing dot and default port removed, and an empty
        path becomes ``/``, so URLs that differ only in those parts collapse to the
        SAME document and match, while pages that differ by query stay apart.  A
        document an older build stored under a former spelling of the URL matches
        too; ``legacy`` says so, and the older name is never returned.  A
        trailing-slash difference on a non-empty path does NOT normalize and
        will not match, and a URL that cannot be stored matches nothing.
        """
        body = await self._authorized_body(request, MAX_CAPTURES_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        url = self._require_text(body, "url")
        if isinstance(url, JSONResponse):
            return url
        collection = await self._collection_for_cwd(self._str_field(body, "cwd"))
        document_name = WebFetchName.maybe(url)
        stored_as = (
            await run_in_threadpool(
                LegacyUrlNames(self.ctx.database).stored_as,
                document_name,
                collection.name,
            )
            if document_name is not None
            else None
        )
        matched = stored_as is not None
        return JSONResponse(
            {
                "matched": matched,
                "document_name": document_name if matched else None,
                "legacy": matched and stored_as != document_name,
            }
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
