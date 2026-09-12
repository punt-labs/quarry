"""Fetch a URL over HTTP(S) and return validated HTML or text."""

from __future__ import annotations

import contextlib
import urllib.request
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, final
from urllib.error import HTTPError, URLError

from quarry.ingestion.ssrf_redirect import GUARDED_OPENER
from quarry.url_safety import UrlSafetyCheck

if TYPE_CHECKING:
    from http.client import HTTPResponse

_ALLOWED_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_USER_AGENT = "quarry/1.0 (+https://github.com/punt-labs/quarry)"


@final
@dataclass(frozen=True, slots=True)
class FetchedBody:
    """The decoded body of an HTTP fetch plus its declared media type.

    ``media_type`` is the lower-cased ``Content-Type`` primary token
    (before any parameters) — ``"text/html"``, ``"application/json"``,
    ``"text/plain"``, or ``""`` when the response omitted the header.
    Callers decide how to route the body: HTML through the extractor,
    everything else through the text pipeline (G4 capture-as-text).
    """

    text: str
    media_type: str

    @property
    def is_html(self) -> bool:
        """Return True when the body is HTML/XHTML or Content-Type was absent.

        A missing ``Content-Type`` is treated as HTML for backward
        compatibility with servers that omit the header — the pre-G4
        contract for :meth:`WebFetcher.fetch`.
        """
        return not self.media_type or self.media_type in _ALLOWED_MEDIA_TYPES


# Bound one response body.  Mirrors the daemon's 4 MiB capture-body cap
# (``MAX_CAPTURE_BODY_BYTES``); core cannot import the presentation-layer
# constant, so the policy is restated here.  A body past the cap fails cleanly
# instead of streaming without limit.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


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

        Unlike :meth:`fetch`, does NOT reject non-HTML content — the
        caller decides how to route JSON, plain text, or XHTML through
        the pipeline.  Underlying safety checks (SSRF, size cap,
        deadline) still apply.

        Raises:
            ValueError: URL is not HTTP(S), the final URL is unsafe, or
                the body exceeds the size cap.
            OSError: Network error.
            TimeoutError: Total-time deadline exceeded.
        """
        if not url.lower().startswith(("http://", "https://")):
            msg = f"Only HTTP(S) URLs are supported: {url}"
            raise ValueError(msg)
        # Self-gate the initial URL (host + resolved address) BEFORE opening the
        # socket -- defence in depth behind the route boundary, and so a direct
        # caller cannot reach an internal address.  Redirect hops are gated by
        # GUARDED_OPENER; this covers the first hop.
        reason = UrlSafetyCheck.reject_reason(url)
        if reason is not None:
            msg = f"URL rejected: {reason}"
            raise ValueError(msg)

        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"User-Agent": _USER_AGENT},
        )
        deadline = monotonic() + self.timeout + self._DEADLINE_MARGIN_S
        try:
            with GUARDED_OPENER.open(request, timeout=self.timeout) as resp:
                return self._decode_body(resp, deadline)
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

    @staticmethod
    def _decode_body(resp: HTTPResponse, deadline: float) -> FetchedBody:
        """Validate the final URL, decode the body, return it with its media type."""
        WebFetcher._check_final_url(resp)
        charset = resp.headers.get_content_charset() or "utf-8"
        body = WebFetcher._read_body(resp, deadline)
        media_type = WebFetcher._media_type(resp)
        text = body.decode(charset, errors="replace")
        return FetchedBody(text=text, media_type=media_type)

    @staticmethod
    def _check_final_url(resp: HTTPResponse) -> None:
        """Raise if the final URL (post-redirect) is unsafe/internal."""
        reason = UrlSafetyCheck.reject_reason(resp.url)
        if reason is not None:
            msg = f"final URL rejected: {reason}"
            raise ValueError(msg)

    @staticmethod
    def _media_type(resp: HTTPResponse) -> str:
        """Return the primary media type from the ``Content-Type`` header."""
        content_type: str = resp.headers.get("Content-Type", "")
        return content_type.split(";", 1)[0].strip().lower()

    @staticmethod
    def _read_body(resp: HTTPResponse, deadline: float) -> bytes:
        """Read the body in chunks under the size cap and wall-clock deadline.

        The deadline and the running total are both checked after each read
        returns: a slow-drip body is aborted once the deadline has passed, and an
        oversize body fails without being buffered whole.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in iter(partial(resp.read, _READ_CHUNK_BYTES), b""):
            if monotonic() > deadline:
                msg = "fetch exceeded its total time budget"
                raise TimeoutError(msg)
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                msg = f"response exceeds {_MAX_RESPONSE_BYTES}-byte cap"
                raise ValueError(msg)
            chunks.append(chunk)
        return b"".join(chunks)
