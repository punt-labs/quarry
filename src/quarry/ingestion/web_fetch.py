"""Fetch a URL over HTTP(S) and return validated HTML or text."""

from __future__ import annotations

import contextlib
import urllib.request
from dataclasses import dataclass, field
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, Self, final
from urllib.error import HTTPError, URLError

from quarry.ingestion.fetched_body import FetchedBody, FetchGate
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

    def media_type(self) -> str:
        """Return the primary media type from the ``Content-Type`` header."""
        content_type: str = self._resp.headers.get("Content-Type", "")
        return content_type.split(";", 1)[0].strip().lower()

    def decode(self) -> str:
        """Return the body decoded leniently, read under the cap and deadline."""
        charset = self._resp.headers.get_content_charset() or "utf-8"
        return self._read().decode(charset, errors="replace")

    def _read(self) -> bytes:
        """Read the body in chunks under the size cap and wall-clock deadline.

        The deadline and the running total are both checked after each read
        returns: a slow-drip body is aborted once the deadline has passed, and an
        oversize body fails without being buffered whole.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in iter(partial(self._resp.read, _READ_CHUNK_BYTES), b""):
            if monotonic() > self._deadline:
                msg = "fetch exceeded its total time budget"
                raise TimeoutError(msg)
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                msg = f"response exceeds {_MAX_RESPONSE_BYTES}-byte cap"
                raise ValueError(msg)
            chunks.append(chunk)
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
        ``ingest_url`` on a user-initiated URL).  The G4 capture path
        uses :meth:`fetch_body` instead.

        Raises:
            ValueError: If the URL is not HTTP(S), the response is not HTML, or
                the body exceeds the size cap.
            OSError: On network errors.
            TimeoutError: Total-time deadline exceeded (propagated from
                :meth:`fetch_body`).
        """
        body = self.fetch_body(url)
        if not body.is_html:
            msg = f"URL returned non-HTML content: {body.media_type or '(unknown)'}"
            raise ValueError(msg)
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
            ValueError: URL is not HTTP(S), the final URL is unsafe, or
                the body exceeds the size cap.
            OSError: Network error.
            TimeoutError: Total-time deadline exceeded.
        """
        self.gate.check_url(url)

        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"User-Agent": _USER_AGENT},
        )
        deadline = monotonic() + self.timeout + self._DEADLINE_MARGIN_S
        try:
            with GUARDED_OPENER.open(request, timeout=self.timeout) as resp:
                return self._decode_body(_OpenedResponse(resp, deadline))
        except HTTPError as exc:
            # HTTPError IS an open response holding a socket fd; close it before
            # re-raising or a failed fetch leaks an fd (EMFILE over a crawl).
            msg = f"HTTP {exc.code} fetching {url}"
            with contextlib.suppress(OSError, ValueError):
                exc.close()
            raise ValueError(msg) from exc
        except URLError as exc:
            msg = f"Cannot reach {url}: {exc.reason}"
            raise OSError(msg) from exc
        except TimeoutError as exc:
            # Name the URL so concurrent fetches are distinguishable in logs; the
            # size-cap ValueError already carries its own context and is left as is.
            msg = f"Timed out fetching {url} (exceeded total time budget)"
            raise TimeoutError(msg) from exc

    def _decode_body(self, opened: _OpenedResponse) -> FetchedBody:
        """Validate the URL and media type, then decode the body.

        The media type is checked before the body is read, so a rejected download
        costs no bandwidth.
        """
        self.gate.check_final_url(opened.url)
        media_type = opened.media_type()
        self.gate.check_media_type(media_type)
        return FetchedBody(text=opened.decode(), media_type=media_type)
