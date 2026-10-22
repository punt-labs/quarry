"""Fetch a captured URL and route its body; the labels a daemon ingest carries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, cast, final, get_args

from quarry.capture_url import CaptureUrl
from quarry.content_guard import UnindexableContentError
from quarry.ingestion import ingest_context, web_ingest
from quarry.ingestion.ingest_context import IngestContext, MemoryType
from quarry.ingestion.web_fetch import WebFetcher
from quarry.scrub import scrub_and_log

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.ingestion.fetched_body import FetchedBody


class LabelledJob(Protocol):
    """A daemon job carrying the plain-string labels of an ingest."""

    @property
    def overwrite(self) -> bool: ...
    @property
    def collection(self) -> str: ...
    @property
    def agent_handle(self) -> str: ...
    @property
    def memory_type(self) -> str: ...
    @property
    def summary(self) -> str: ...


@final
@dataclass(frozen=True, slots=True)
class IngestLabels:
    """Where an ingest writes and how it is tagged.

    The job dataclasses carry the daemon boundary's plain strings; this is the one
    place those become the pipeline's :class:`IngestContext`, so the
    ``memory_type`` narrowing to its storage vocabulary happens exactly once.
    """

    overwrite: bool
    collection: str
    agent_handle: str
    memory_type: MemoryType
    summary: str

    _MEMORY_TYPES: ClassVar[tuple[str, ...]] = get_args(MemoryType.__value__)

    @classmethod
    def memory_type_of(cls, raw: str) -> MemoryType:
        """Return *raw* as a storage ``memory_type``, or raise ``ValueError``."""
        if raw not in cls._MEMORY_TYPES:
            msg = f"unknown memory_type {raw!r}"
            raise ValueError(msg)
        return cast("MemoryType", raw)

    @classmethod
    def from_job(cls, job: LabelledJob) -> Self:
        """Build labels from a job's plain-string fields."""
        return cls.of(
            overwrite=job.overwrite,
            collection=job.collection,
            agent_handle=job.agent_handle,
            memory_type=job.memory_type,
            summary=job.summary,
        )

    @classmethod
    def of(
        cls,
        *,
        overwrite: bool,
        collection: str,
        agent_handle: str,
        memory_type: str,
        summary: str,
    ) -> Self:
        """Build labels from the daemon boundary's plain strings."""
        return cls(
            overwrite,
            collection,
            agent_handle,
            cls.memory_type_of(memory_type),
            summary,
        )

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
    keeps that name instead of falling back to the raw URL.
    """

    __slots__ = ("_fetcher", "_labels", "_name_override", "_source")

    _source: str
    _labels: IngestLabels
    _name_override: str
    _fetcher: WebFetcher

    _MARKER_UNSAFE: ClassVar[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9/.+;=_-]")
    _MARKER_MAX_LEN: ClassVar[int] = 128
    _MARKER_FALLBACK: ClassVar[str] = "application/octet-stream"

    def __new__(
        cls,
        source: str,
        labels: IngestLabels,
        name_override: str = "",
        fetcher: WebFetcher | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._labels = labels
        self._name_override = name_override
        self._fetcher = fetcher or WebFetcher()
        return self

    @staticmethod
    def redacted(source: str) -> str:
        """Return *source* with userinfo, query and fragment dropped, for a log line."""
        return CaptureUrl.for_web_fetch(source)

    def fetch(self) -> FetchedBody:
        """Fetch ``source``; every failure is a URL-free :class:`FetchRefusedError`."""
        return self._fetcher.fetch_body(self._source)

    def run(
        self, ctx: DaemonContext, *, require_text: bool = False
    ) -> dict[str, object]:
        """Fetch and store ``source``, returning the ingest result.

        Raises:
            UnindexableContentError: *require_text* is set and nothing was stored.
        """
        result = self.ingest(ctx, self.fetch())
        if require_text and not result.get("chunks"):
            msg = "capture stored nothing: no text extracted"
            raise UnindexableContentError(msg)
        return result

    def ingest(self, ctx: DaemonContext, body: FetchedBody) -> dict[str, object]:
        """Route a fetched body: HTML via extractor, else text with a mime marker.

        The non-HTML branch derives its ``document_name`` from
        :meth:`CaptureUrl.redacted` rather than passing the raw URL: the
        pipeline's regex scrubber (``_scrub_metadata``) does not know URL
        structure and leaves ``?email=``, ``?token=``, and ``user:pass@``
        components on the persisted ``document_name`` (CWE-532).  The HTML
        branch already routes through :func:`ingest_url`, which does the same
        derivation internally, so the two branches agree.
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

        media_type = self.marker_media_type(body.media_type)
        content = f"<!-- media_type: {media_type} -->\n{body.text}"
        name = self._name_override or CaptureUrl(self._source).redacted(scrub)
        inline = web_ingest.InlineIngest(
            content, format_hint="markdown", content_scrubber=scrub
        )
        return dict(web_ingest.ingest_content(inline, name, progress, context))

    @classmethod
    def marker_media_type(cls, raw: str) -> str:
        """Return a marker-safe media type; fall back on empty/unsafe input.

        ``raw`` is the ``Content-Type`` primary token from the HTTP response and
        is server-controlled.  Interpolating it verbatim into the
        ``<!-- media_type: X -->`` capture marker would let a header carrying
        whitespace, control bytes, or the ``-->`` needle break the single-line
        HTML-comment contract or escape the marker entirely.  Strip to the
        RFC 6838-like shape (alphanumerics plus ``/.+;=_-``), cap at 128 chars,
        and substitute ``application/octet-stream`` when the input degrades to
        nothing after cleaning — the marker survives; the injection does not.
        """
        cleaned = cls._MARKER_UNSAFE.sub("", raw)[: cls._MARKER_MAX_LEN]
        return cleaned or cls._MARKER_FALLBACK
