"""An open HTTP response, bound to the wall-clock deadline and size cap of its fetch."""

from __future__ import annotations

import urllib.request
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, Self, final

from quarry.http_url import HttpUrl
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.ssrf_redirect import GUARDED_OPENER

if TYPE_CHECKING:
    from http.client import HTTPResponse

# Bound one response body.  Mirrors the daemon's 4 MiB capture-body cap
# (``MAX_CAPTURE_BODY_BYTES``); core cannot import the presentation-layer
# constant, so the policy is restated here.  A body past the cap fails cleanly
# instead of streaming without limit.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


@final
class OpenedResponse:
    """An open HTTP response bound to the wall-clock deadline of its fetch."""

    __slots__ = ("_deadline", "_resp")

    _resp: HTTPResponse
    _deadline: float

    def __new__(cls, resp: HTTPResponse, deadline: float) -> Self:
        self = super().__new__(cls)
        self._resp = resp
        self._deadline = deadline
        return self

    @classmethod
    def open(
        cls, request: urllib.request.Request, timeout: int, deadline: float
    ) -> Self:
        """Open *request* through the guarded opener, or raise a URL-free refusal.

        Raises:
            FetchRefusedError: A transport or policy failure (any other exception
                is a bug and propagates).  The
                library's own message quotes the URL, so it is replaced, never
                chained.
        """
        try:
            resp = GUARDED_OPENER.open(request, timeout=timeout)
        except FetchRefusedError.REFUSABLE as exc:
            raise FetchRefusedError.from_transfer_failure(
                exc, subject=HttpUrl.shown(request.full_url)
            ) from None
        return cls(resp, deadline)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._resp.close()

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
        except LookupError:
            shown = "".join(c for c in charset if c.isalnum() or c in "-_.:")[:40]
            msg = f"content rejected: unsupported charset {shown}"
            raise FetchRefusedError(msg) from None

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
        except FetchRefusedError.REFUSABLE as exc:
            raise FetchRefusedError.from_transfer_failure(
                exc, subject=HttpUrl.shown(self._resp.url)
            ) from None
        return b"".join(chunks)
