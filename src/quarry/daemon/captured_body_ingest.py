"""Fetch a captured URL and route its body; the labels a daemon ingest carries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.capture_marker import CaptureMarker
from quarry.content_guard import UnindexableContentError
from quarry.ingestion import ingest_context, web_ingest
from quarry.ingestion.ingest_context import IngestContext, MemoryType
from quarry.ingestion.web_fetch import WebFetcher
from quarry.scrub import scrub_and_log

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.http_url import HttpUrl
    from quarry.ingestion.fetched_body import FetchedBody

# WebFetcher is frozen, so one shared instance carries the default policy.
_DEFAULT_FETCHER = WebFetcher()


@final
@dataclass(frozen=True, slots=True)
class IngestLabels:
    """Where an ingest writes and how it is tagged, validated once at the route.

    Routes build this from the request and the jobs carry it unchanged, so the
    narrowing of ``memory_type`` to its storage vocabulary happens exactly once
    (:class:`MemoryTypeGuard`) and every job turns it into the pipeline's
    :class:`IngestContext` the same way.
    """

    overwrite: bool
    collection: str
    agent_handle: str
    memory_type: MemoryType
    summary: str

    def context(self, ctx: DaemonContext) -> IngestContext:
        """Return the pipeline context writing to *ctx*'s database with these labels."""
        return IngestContext(
            ctx.database,
            ctx.settings,
            overwrite=self.overwrite,
            collection=self.collection,
            agent_handle=self.agent_handle,
            memory_type=self.memory_type,
            summary=self.summary,
        )


@final
class CapturedBodyIngest:
    """One scrubbed capture of a URL: fetch it, then store the body by media type.

    Shared by the URL-ingest job and the capture job's empty-inline fallback, so
    both agree on the media-type contract: an HTML body flows through
    :func:`ingest_url` with the already-fetched text reused via
    ``prefetched_html`` (no double fetch), and anything else is stored via
    :func:`ingest_content` with a leading ``<!-- media_type: X -->`` marker so a
    reader — and a downstream grep — knows the shape.

    A fetch that fails or is refused, or a body that cannot be indexed, raises
    :class:`UnindexableContentError` with a URL-free reason; nothing is returned
    for a failure, so no caller can report an empty success.  ``name_override`` is
    empty for the primary URL route (the URL is the natural name); a capture
    re-fetch sets it to the inline capture's friendly name so a non-HTML capture
    keeps that name instead of falling back to the URL's stored name.
    """

    __slots__ = ("_fetcher", "_labels", "_name_override", "_source")

    _source: HttpUrl
    _labels: IngestLabels
    _name_override: str
    _fetcher: WebFetcher

    def __new__(
        cls,
        source: HttpUrl,
        labels: IngestLabels,
        name_override: str = "",
        fetcher: WebFetcher = _DEFAULT_FETCHER,
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._labels = labels
        self._name_override = name_override
        self._fetcher = fetcher
        return self

    def subject(self) -> str:
        """Return the source's log form, ``scheme://host[:port]/…(N segments)``."""
        return str(self._source)

    def fetch(self) -> FetchedBody:
        """Fetch ``source``; any failure is a URL-free ``UnindexableContentError``."""
        return self._fetcher.fetch_body(self._source)

    def run(self, ctx: DaemonContext) -> dict[str, object]:
        """Fetch and store ``source``, returning the ingest result.

        Raises:
            UnindexableContentError: The fetch failed or was refused, or nothing
                was stored: a page that extracts to no text is not a success.
        """
        result = self.ingest(ctx, self.fetch())
        if not result.get("chunks"):
            msg = "capture stored nothing: no text extracted"
            raise UnindexableContentError(msg)
        return result

    def ingest(self, ctx: DaemonContext, body: FetchedBody) -> dict[str, object]:
        """Route a fetched body: HTML via extractor, else text with a mime marker.

        The non-HTML branch names the document with
        :attr:`HttpUrl.document_name` rather than the raw URL: the pipeline's
        regex scrubber (``_scrub_metadata``) does not know URL structure and
        would leave ``?email=`` and ``?token=`` on the persisted name
        (CWE-532).  Userinfo never gets this far (admission refuses it), and
        query, fragment and ``;params`` are dropped.  The HTML branch routes
        through :func:`ingest_url`, which names the document the same way, so the
        two branches agree.
        """

        def scrub(text: str) -> str:
            return scrub_and_log(text, "web-fetch")

        context = self._labels.context(ctx)
        progress = ingest_context.Progress(None)
        if body.is_html:
            request = web_ingest.UrlIngest(
                self._source, content_scrubber=scrub, prefetched_html=body.text
            )
            return dict(web_ingest.ingest_url(request, progress, context))

        content = CaptureMarker(body.media_type).render() + body.text
        name = self._name_override or scrub(self._source.document_name)
        inline = web_ingest.InlineIngest(
            content, format_hint="markdown", content_scrubber=scrub
        )
        return dict(web_ingest.ingest_content(inline, name, progress, context))
