"""Fetch a URL over HTTP(S) and return validated HTML or text.

Every failure leaves this module as a :class:`FetchRefusedError` (or its content
siblings) whose message never embeds the URL, so no caller has to redact it.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from functools import partial
from http.client import HTTPException
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.ingestion.fetched_body import FetchedBody, FetchGate, FetchRefusedError
from quarry.ingestion.ssrf_redirect import GUARDED_OPENER

if TYPE_CHECKING:
    from http.client import HTTPResponse

_USER_AGENT = "quarry/1.0 (+https://github.com/punt-labs/quarry)"


# Bound one response body.  Mirrors the daemon's 4 MiB capture-body cap
# (``MAX_CAPTURE_BODY_BYTES``); core cannot import the presentation-layer
# constant, so the policy is restated here.  A body past the cap fails cleanly
# instead of streaming without limit.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


@final
class _OpenedResponse:
    """An open HTTP response bound to the wall-clock deadline of its fetch."""

    __slots__ = ("_deadline", "_resp")

    _resp: HTTPResponse
    _deadline: float

    def __new__(cls, resp: HTTPResponse, deadline: float) -> Self:
        self = super().__new__(cls)
        self._resp = resp
        self._deadline = deadline
        return self

    @property
    def url(self) -> str:
        """Return the final URL, after any redirects."""
        return self._resp.url

    def content_type(self) -> str:
        """Return the raw ``Content-Type`` header ("" if absent)."""
        content_type: str = self._resp.headers.get("Content-Type", "")
        return content_type

    def decode(self) -> str:
        """Return the body decoded leniently, read under the cap and deadline.

        Raises:
            FetchRefusedError: The charset is unknown or the body is over the cap.
        """
        charset = self._resp.headers.get_content_charset() or "utf-8"
        try:
            return self._read().decode(charset, errors="replace")
        except LookupError as exc:
            shown = "".join(c for c in charset if c.isalnum() or c in "-_.:")[:40]
            msg = f"content rejected: unsupported charset {shown}"
            raise FetchRefusedError(msg) from exc

    def _read(self) -> bytes:
        """Read the body in chunks under the size cap and wall-clock deadline.

        The deadline and the running total are both checked after each read
        returns: a slow-drip body is aborted once the deadline has passed, and an
        oversize body fails without being buffered whole.
        """
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in iter(partial(self._resp.read, _READ_CHUNK_BYTES), b""):
                if monotonic() > self._deadline:
                    msg = "fetch failed: exceeded its total time budget"
                    raise FetchRefusedError(msg)
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    cap = f"{_MAX_RESPONSE_BYTES}-byte cap"
                    msg = f"content rejected: response exceeds {cap}"
                    raise FetchRefusedError(msg)
                chunks.append(chunk)
        except (OSError, HTTPException) as exc:
            raise FetchGate.translate(exc) from exc
        return b"".join(chunks)


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

    def fetch(self, url: str) -> str:
        """Fetch *url* and return the decoded HTML body.

        Preserved for callers that require HTML (a sitemap crawl,
        ``ingest_url`` on a user-initiated URL).  The capture path uses
        :meth:`fetch_body` instead.

        Raises:
            FetchRefusedError: Anything :meth:`fetch_body` raises, or the response
                is not HTML.
        """
        body = self.fetch_body(url)
        if not body.is_html:
            msg = f"content rejected: non-HTML content {body.label}"
            raise FetchRefusedError(msg)
        return body.text

    def fetch_body(self, url: str) -> FetchedBody:
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
        self.gate.check_url(url)

        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"User-Agent": _USER_AGENT},
        )
        deadline = monotonic() + self.timeout + self._DEADLINE_MARGIN_S
        try:
            response = GUARDED_OPENER.open(request, timeout=self.timeout)
        except (OSError, ValueError) as exc:
            raise self.gate.translate(exc) from exc
        with response as resp:
            return self._decode_body(_OpenedResponse(resp, deadline))

    def _decode_body(self, opened: _OpenedResponse) -> FetchedBody:
        """Validate the URL and media type, then decode the body.

        The media type is checked before the body is read, so a rejected body is
        never read.
        """
        self.gate.check_final_url(opened.url)
        content_type = opened.content_type()
        self.gate.check_media_type(content_type)
        return FetchedBody.from_header(opened.decode(), content_type)
