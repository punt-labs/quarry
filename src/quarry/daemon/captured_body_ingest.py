"""Fetch a captured URL and route its body by media type."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.capture_marker import CaptureMarker
from quarry.content_guard import UnindexableContentError
from quarry.ingestion import ingest_context, web_ingest
from quarry.ingestion.existing_page import ExistingPage
from quarry.ingestion.web_fetch import WebFetcher
from quarry.scrub import scrub_and_log

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_labels import IngestLabels
    from quarry.http_url import HttpUrl
    from quarry.ingestion.fetched_body import FetchedBody

# WebFetcher is frozen, so one shared instance carries the default policy.
_DEFAULT_FETCHER = WebFetcher()


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
    for a failure, so no caller can report an empty success.  The document is
    named for the URL, and a capture re-fetch only happens for a page capture, which
    the capture route has already matched to that name, so both branches write the
    same document.
    """

    __slots__ = ("_fetcher", "_labels", "_source")

    _source: HttpUrl
    _labels: IngestLabels
    _fetcher: WebFetcher

    def __new__(
        cls,
        source: HttpUrl,
        labels: IngestLabels,
        fetcher: WebFetcher = _DEFAULT_FETCHER,
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._labels = labels
        self._fetcher = fetcher
        return self

    def existing(self, ctx: DaemonContext) -> ExistingPage | None:
        """Return the stored capture of ``source`` that makes a re-ingest a skip."""
        return ExistingPage.capture(self._source, self._labels.context(ctx))

    def subject(self) -> str:
        """Return the source's log form, ``scheme://host[:port]/…(segments=N)``."""
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
        (CWE-532).  Userinfo never gets this far (admission refuses it); the
        fragment and ``;params`` are dropped and the query is kept only as a
        digest.  The HTML branch routes
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
        name = scrub(self._source.document_name)
        inline = web_ingest.InlineIngest(
            content, format_hint="markdown", content_scrubber=scrub, url_sourced=True
        )
        return dict(web_ingest.ingest_content(inline, name, progress, context))
