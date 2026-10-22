"""Fetch a captured URL and route its body: HTML through the extractor, else text."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, cast, final

from quarry.capture_url import CaptureUrl
from quarry.content_guard import UnindexableContentError, UnsupportedContentError
from quarry.ingestion.fetched_body import MediaTypePolicy
from quarry.ingestion.web_fetch import WebFetcher

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.ingestion.fetched_body import FetchedBody
    from quarry.ingestion.ingest_context import MemoryType

logger = logging.getLogger(__name__)

# ``fetch_body`` messages are of two shapes: policy rejections that describe a
# rule (``"URL rejected: ..."``, ``"final URL rejected: ..."``, and the
# media-type refusal ``"content rejected: ..."``) and network/HTTP failures that
# quote the raw URL (``"Cannot reach {url}: ..."``).  Only the first shape is
# safe to append to the log — the second would carry ``?token=``/``user:pass@``
# secrets into the persistent quarry.log (CWE-532).
_SAFE_FETCH_ERROR_PREFIXES: Final = (
    "URL rejected:",
    "final URL rejected:",
    "content rejected:",
)


@final
@dataclass(frozen=True, slots=True)
class CapturedBodyIngest:
    """One scrubbed capture of a URL: fetch it, then store the body by media type.

    Shared by the URL-ingest job and the capture job's empty-inline fallback, so
    both agree on the media-type contract: an HTML body flows through
    :func:`ingest_url` with the already-fetched text reused via
    ``prefetched_html`` (no double fetch), and anything else is stored via
    :func:`ingest_content` with a leading ``<!-- media_type: X -->`` marker so a
    reader — and a downstream grep — knows the shape.

    ``name_override`` is empty for the primary URL route (the URL is the natural
    name); a capture re-fetch sets it to the inline capture's friendly name so a
    non-HTML capture keeps that name instead of falling back to the raw URL.
    """

    source: str
    overwrite: bool
    collection: str
    agent_handle: str
    memory_type: str
    summary: str
    name_override: str = ""

    def fetch(self, kind: str) -> FetchedBody | None:
        """Fetch ``source``, or return ``None`` after logging the failure redacted.

        ``fetch_body`` raises ``OSError``/``ValueError``/``TimeoutError`` with the
        raw URL embedded in the message (``"Cannot reach {url}: ..."``); letting
        that reach the log or ``task_terminal``'s traceback leaks ``?token=`` and
        ``user:pass@`` secrets into the persistent quarry.log (CWE-532).  Redact
        the URL through the normaliser writes use, render the failure through
        :meth:`_classify_fetch_error`, and return ``None`` so the caller
        short-circuits with an empty result instead of a traceback.

        A media-type refusal is not a transient failure: it carries a URL-free
        reason, so it propagates for the caller to report.

        Raises:
            UnsupportedContentError: The URL serves a media type that is not text.
        """
        try:
            return WebFetcher().fetch_body(self.source)
        except UnsupportedContentError:
            raise
        except (OSError, ValueError, TimeoutError) as exc:
            logger.warning(
                "%s of %s failed (%s); skipping",
                kind,
                CaptureUrl.for_web_fetch(self.source),
                self._classify_fetch_error(exc),
            )
            return None

    def run(self, ctx: DaemonContext, kind: str) -> dict[str, object]:
        """Fetch and store ``source``; a failure or rejection is a result, not a raise.

        A fetch failure is an empty result (logged by :meth:`fetch`); a refused
        media type or binary body is :meth:`guarded`.
        """
        return self.guarded(kind, partial(self._fetch_and_ingest, ctx, kind))

    def guarded(
        self, kind: str, action: Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        """Run *action*, reporting an unindexable body as an empty ``rejected`` result.

        On success the result is *action*'s own.  On a refusal it is
        ``{"chunks": 0, "sections": 0, "rejected": <reason>}``, with one redacted
        WARN and no traceback: the reason is URL-free, the URL is redacted, and the
        caller decides whether ``rejected`` fails a task.
        """
        try:
            return action()
        except UnindexableContentError as exc:
            url = CaptureUrl.for_web_fetch(self.source)
            logger.warning("%s of %s rejected: %s", kind, url, exc)
            return {"chunks": 0, "sections": 0, "rejected": str(exc)}

    def _fetch_and_ingest(self, ctx: DaemonContext, kind: str) -> dict[str, object]:
        body = self.fetch(kind)
        if body is None:
            return {"chunks": 0, "sections": 0}
        return self.ingest(ctx, body)

    def ingest(self, ctx: DaemonContext, body: FetchedBody) -> dict[str, object]:
        """Route a fetched body: HTML via extractor, else text with a mime marker.

        The non-HTML branch derives its ``document_name`` from
        :meth:`CaptureUrl.redacted` rather than passing the raw URL: the
        pipeline's regex scrubber (``_scrub_metadata``) does not know URL
        structure and leaves ``?email=``, ``?token=``, and ``user:pass@``
        components on the persisted ``document_name`` (CWE-532).  The HTML
        branch already routes through :func:`ingest_url`, which does the same
        derivation internally, so the two branches now match.
        """
        from quarry.ingestion.ingest_context import (  # noqa: PLC0415
            IngestContext,
            Progress,
        )
        from quarry.ingestion.web_ingest import (  # noqa: PLC0415
            InlineIngest,
            UrlIngest,
            ingest_content,
            ingest_url,
        )
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        def scrub(text: str) -> str:
            return scrub_and_log(text, "web-fetch")

        context = IngestContext(
            ctx.database,
            ctx.settings,
            overwrite=self.overwrite,
            collection=self.collection,
            agent_handle=self.agent_handle,
            memory_type=cast("MemoryType", self.memory_type),
            summary=self.summary,
        )

        if body.is_html:
            request = UrlIngest(
                self.source, content_scrubber=scrub, prefetched_html=body.text
            )
            return dict(ingest_url(request, Progress(None), context))

        media_type = MediaTypePolicy.sanitize(body.media_type)
        content = f"<!-- media_type: {media_type} -->\n{body.text}"
        name = self.name_override or CaptureUrl(self.source).redacted(scrub)
        inline = InlineIngest(content, format_hint="markdown", content_scrubber=scrub)
        return dict(ingest_content(inline, name, Progress(None), context))

    @staticmethod
    def _classify_fetch_error(exc: BaseException) -> str:
        """Return the log-safe rendering of a ``fetch_body`` exception.

        Two message shapes are known-safe -- the url-safety rejections raised
        by ``fetch_body`` and ``_check_final_url`` describe a policy rule and
        do not embed the raw URL -- and their text is appended so an operator
        sees WHY the URL was rejected (private IP, redirect to blocked host,
        metadata hostname) without having to reproduce with debug logging.
        Every other exception falls back to the class name alone, preserving
        the CWE-532 guard for network/HTTP failures whose messages quote the
        raw URL (``"Cannot reach {url}: ..."``, ``"HTTP N fetching {url}"``).
        """
        msg = str(exc)
        cls = type(exc).__name__
        if msg.startswith(_SAFE_FETCH_ERROR_PREFIXES):
            return f"{cls}: {msg}"
        return cls
