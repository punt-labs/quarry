"""Fetch a URL over HTTP(S) and return validated HTML text."""

from __future__ import annotations

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
        """Fetch *url* and return the decoded response body.

        Raises:
            ValueError: If the URL is not HTTP(S), the response is not HTML, or
                the body exceeds the size cap.
            OSError: On network errors or once the total-time deadline passes.
        """
        if not url.startswith(("http://", "https://")):
            msg = f"Only HTTP(S) URLs are supported: {url}"
            raise ValueError(msg)

        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"User-Agent": _USER_AGENT},
        )
        deadline = monotonic() + self.timeout + self._DEADLINE_MARGIN_S
        try:
            with GUARDED_OPENER.open(request, timeout=self.timeout) as resp:
                return self._decode_html(resp, deadline)
        except HTTPError as exc:
            msg = f"HTTP {exc.code} fetching {url}"
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
    def _decode_html(resp: HTTPResponse, deadline: float) -> str:
        """Validate the response, then decode the bounded body to text."""
        WebFetcher._reject_non_html(resp)
        charset = resp.headers.get_content_charset() or "utf-8"
        body = WebFetcher._read_body(resp, deadline)
        return body.decode(charset, errors="replace")

    @staticmethod
    def _reject_non_html(resp: HTTPResponse) -> None:
        """Raise if the final URL is unsafe (non-HTTP(S)/internal) or non-HTML."""
        reason = UrlSafetyCheck.reject_reason(resp.url)
        if reason is not None:
            msg = f"final URL rejected: {reason}"
            raise ValueError(msg)
        content_type: str = resp.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type and media_type not in _ALLOWED_MEDIA_TYPES:
            msg = f"URL returned non-HTML content: {content_type}"
            raise ValueError(msg)

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
