"""Fetch a URL over HTTP(S) and return validated HTML or text.

Every failure leaves this module as a :class:`FetchRefusedError` (or its content
siblings) whose message never embeds userinfo, path or query, so no caller has
to redact it.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, final

from quarry.ingestion.fetched_body import FetchedBody, FetchGate
from quarry.ingestion.opened_response import OpenedResponse

if TYPE_CHECKING:
    from quarry.http_url import HttpUrl

_USER_AGENT = "quarry/1.0 (+https://github.com/punt-labs/quarry)"


@final
@dataclass(frozen=True, slots=True)
class WebFetcher:
    """Fetches HTML over HTTP(S), rejecting non-HTTP schemes and non-HTML bodies.

    A small value object that keeps the fetch policy — allowed schemes, allowed
    media types, redirect validation, and the User-Agent — in one place rather
    than inlined in the ingestion pipeline.

    Two bounds keep a single fetch finite so it can never hold the ingest
    queue's embed gate open indefinitely (DES-042): ``timeout`` bounds each
    socket operation, and a total wall-clock deadline (plus a response-size cap)
    bounds the fetch as a whole.  The deadline is checked between reads — after
    each returns, never mid-read — so a single in-flight read is not interrupted;
    a slow-drip server that satisfies every per-op timeout yet never finishes is
    caught at the next check, bounding the whole fetch to roughly the deadline
    plus one socket timeout.  An unbounded body fails at the size cap.
    """

    timeout: int = 30
    gate: FetchGate = field(default_factory=FetchGate)

    _DEADLINE_MARGIN_S: ClassVar[float] = 1.0

    def fetch(self, url: HttpUrl) -> str:
        """Fetch *url* and return the decoded HTML body.

        Preserved for callers that require HTML (a sitemap crawl,
        ``ingest_url`` on a user-initiated URL).  The capture path uses
        :meth:`fetch_body` instead.

        Raises:
            UnindexableContentError: A :class:`FetchRefusedError` or
                :class:`UnsupportedContentError` from :meth:`fetch_body`, or a
                ``FetchRefusedError`` if the response is not HTML.
        """
        return self.fetch_body(url).html_text()

    def fetch_body(self, url: HttpUrl) -> FetchedBody:
        """Fetch *url* and return its decoded body + declared media type.

        Unlike :meth:`fetch`, does NOT reject non-HTML text — the caller
        decides how to route JSON, plain text, or XHTML through the pipeline.
        A media type outside :class:`MediaTypePolicy` (a PDF, an image) is
        refused from the headers alone, before the body is read.  Underlying
        safety checks (SSRF, size cap, deadline) still apply.

        Raises:
            UnsupportedContentError: The media type is not indexable text.
            FetchRefusedError: The URL, a redirect hop, or the transfer failed, or
                the body is over the cap or in an unknown charset.
        """
        self.gate.check_url(url.fetch_url)

        request = urllib.request.Request(  # noqa: S310
            url.fetch_url,
            headers={"User-Agent": _USER_AGENT},
        )
        deadline = monotonic() + self.timeout + self._DEADLINE_MARGIN_S
        with OpenedResponse.open(request, self.timeout, deadline) as opened:
            return self._decode_body(opened)

    def _decode_body(self, opened: OpenedResponse) -> FetchedBody:
        """Validate the URL and media type, then decode the body.

        The media type is checked before the body is read, so a rejected body is
        never read.
        """
        self.gate.check_final_url(opened.url)
        content_type = opened.content_type()
        self.gate.check_media_type(content_type)
        return FetchedBody(opened.decode(), content_type)
