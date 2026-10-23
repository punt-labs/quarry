"""A USP web client that runs the SSRF gate on every URL it fetches.

``ultimate-sitemap-parser`` fetches sitemap-indexes, ``robots.txt``, and nested
sub-sitemaps server-side and recurses through them BEFORE quarry ever sees a
leaf page URL.  Its default ``RequestsWebClient`` is ungated, so an index entry,
a ``robots.txt`` ``Sitemap:`` line, or a redirect on the sitemap fetch pointing
at a private, loopback, link-local, CGNAT, or metadata address would be fetched
against the internal service.  Gating the flattened leaf URLs after the crawl
is too late — the internal fetches already happened during recursion.

:class:`GatedSitemapWebClient` closes that hole at USP's fetch boundary: it runs
``UrlAdmission`` on the initial URL and, through :data:`GUARDED_OPENER`, on
every redirect hop, and refuses a blocked target fail-closed by returning a
non-retryable error response (USP then skips the URL and does not recurse into
it).  Passed to every USP entry point, it covers index recursion, robots.txt,
and sub-sitemaps at every depth.
"""

from __future__ import annotations

import contextlib
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final
from urllib.error import HTTPError, URLError

from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)

from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.ssrf_redirect import GUARDED_OPENER
from quarry.url_admission import UrlAdmission

if TYPE_CHECKING:
    from http.client import HTTPMessage, HTTPResponse

_USER_AGENT = "quarry-sitemap/1.0 (+https://github.com/punt-labs/quarry)"


@final
@dataclass(frozen=True, slots=True)
class _GatedSuccessResponse(AbstractWebClientSuccessResponse):
    """A successful USP response backed by a urllib fetch through the gate."""

    _status_code: int
    _status_message: str
    _headers: HTTPMessage
    _body: bytes
    _url: str

    def status_code(self) -> int:
        return self._status_code

    def status_message(self) -> str:
        return self._status_message

    def header(self, case_insensitive_name: str) -> str | None:
        return self._headers.get(case_insensitive_name)

    def raw_data(self) -> bytes:
        return self._body

    def url(self) -> str:
        return self._url


@final
class GatedSitemapWebClient(AbstractWebClient):
    """USP web client that SSRF-gates every fetched URL, fail-closed."""

    _timeout: int
    # USP sets this after construction via set_max_response_data_length; None
    # until then, meaning "no cap".
    _max_bytes: int | None

    def __new__(cls, timeout: int = 30) -> Self:
        self = super().__new__(cls)
        self._timeout = timeout
        self._max_bytes = None
        return self

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        self._max_bytes = max_response_data_length

    def get(self, url: str) -> AbstractWebClientResponse:
        """Fetch *url* only if it passes the SSRF gate at every hop.

        Never raises a fetch failure: a blocked target, a blocked redirect hop, or a
        network error is reported as a ``WebClientErrorResponse`` (the USP contract), so
        USP skips the URL instead of fetching it.  SSRF blocks are non-retryable
        so an attacker-listed URL is never retried.
        """
        reason = UrlAdmission.reject_reason(url)
        if reason is not None:
            return self._error(f"SSRF-blocked URL: {reason}", retryable=False)
        try:
            return self._fetch(url)
        except HTTPError as exc:
            # Capture everything USP reads (status code + message) into plain
            # locals and BUILD the response BEFORE closing: HTTPError IS an open
            # response holding a socket fd, and USP later logs response.message()
            # -- a response built from captured values never lazily reads the
            # now-closed fd.  5xx and 429 are transient (retry); other 4xx are
            # permanent.  Then close the fd (or it leaks -> EMFILE -> starvation).
            retryable = self._retryable_http(exc.code)
            error = self._error(f"HTTP {exc.code}", retryable=retryable)
            with contextlib.suppress(OSError, ValueError):
                # A close failure must not break get()'s never-raises contract;
                # the kernel still frees the fd regardless.
                exc.close()
            return error
        except (TimeoutError, URLError) as exc:
            return self._error(f"unreachable: {type(exc).__name__}", retryable=True)
        except FetchRefusedError.REFUSABLE as exc:
            # Classified once by FetchRefusedError, which logs it.  Any other
            # exception is a bug and propagates, as on the URL ingest path.
            refusal = FetchRefusedError.from_transfer_failure(exc)
            return self._error(str(refusal), retryable=False)

    def _fetch(self, url: str) -> AbstractWebClientResponse:
        """Open *url* through the guarded opener and gate the final URL."""
        request = urllib.request.Request(  # noqa: S310 — scheme + SSRF gated
            url, headers={"User-Agent": _USER_AGENT}
        )
        with GUARDED_OPENER.open(request, timeout=self._timeout) as resp:
            final = UrlAdmission.reject_reason(resp.url)
            if final is not None:
                reason = f"SSRF-blocked final URL: {final}"
                return self._error(reason, retryable=False)
            message = getattr(resp, "reason", None) or "OK"
            return _GatedSuccessResponse(
                int(resp.status), message, resp.headers, self._read(resp), resp.url
            )

    def _read(self, resp: HTTPResponse) -> bytes:
        """Read the body, capped at the length USP requested (if any)."""
        if self._max_bytes is None:
            return resp.read()
        return resp.read(self._max_bytes)

    @staticmethod
    def _retryable_http(code: int) -> bool:
        """Return whether an HTTP status is transient: 5xx or 429 (rate limit)."""
        return code >= 500 or code == 429

    @staticmethod
    def _error(message: str, *, retryable: bool) -> WebClientErrorResponse:
        """Build a USP error response so USP skips (does not fetch) the URL."""
        return WebClientErrorResponse(message=message, retryable=retryable)
