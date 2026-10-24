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

import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Self, final
from urllib.error import HTTPError, URLError

from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)

from quarry.fetch_bug_record import FetchBugRecord
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.ssrf_redirect import GUARDED_OPENER
from quarry.url_admission import UrlAdmission

if TYPE_CHECKING:
    from http.client import HTTPMessage, HTTPResponse

_USP_NOT_FOUND = f"{HTTPStatus.NOT_FOUND} {HTTPStatus.NOT_FOUND.phrase}"
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
    _bugs: FetchBugRecord

    def __new__(cls, timeout: int = 30) -> Self:
        self = super().__new__(cls)
        self._timeout = timeout
        self._max_bytes = None
        self._bugs = FetchBugRecord()
        return self

    def take_bug(self) -> str:
        """Return the class name of the bug a fetch hit, if any, and forget it."""
        return self._bugs.take()

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        self._max_bytes = max_response_data_length

    def get(self, url: str) -> AbstractWebClientResponse:
        """Fetch *url* only if it passes the SSRF gate at every hop.

        Never raises a fetch failure (see :meth:`_get`).  Anything else it raises is
        a bug: USP catches it and reports an invalid node, so it is recorded here,
        whichever step of the fetch it came from, and re-raised unchanged.
        """
        try:
            return self._get(url)
        except Exception as exc:
            self._bugs.record(exc, url, UrlAdmission.shown)
            raise

    def _get(self, url: str) -> AbstractWebClientResponse:
        """Fetch *url* only if it passes the SSRF gate at every hop.

        Never raises a fetch failure: a blocked target, a blocked redirect hop, or a
        network error is reported as a ``WebClientErrorResponse`` (the USP contract), so
        USP skips the URL instead of fetching it.  SSRF blocks are non-retryable
        so an attacker-listed URL is never retried.
        """
        reason = UrlAdmission.reject_reason(url)
        if reason is not None:
            return self._blocked(f"SSRF-blocked URL: {reason}", url)
        try:
            return self._fetch(url)
        except FetchRefusedError.REFUSABLE as exc:
            # Classified once by FetchRefusedError, which logs it with the redacted
            # subject and closes an HTTPError's socket (or it leaks -> EMFILE).  Only
            # whether USP may retry differs: a 5xx, a 429, a timeout and an
            # unreachable host are transient; a policy rejection, a 4xx and the
            # rest are not.  Any other exception is a bug and propagates, as on the
            # URL ingest path.
            retryable = self._is_transient(exc)
            refusal = FetchRefusedError.from_transfer_failure(
                exc, subject=UrlAdmission.shown(url), reported_by_caller=True
            )
            # USP demotes a 404 on a speculative well-known path only when the
            # message is exactly its own "404 Not Found": it, not we, knows which
            # fetches are speculative, so a 404 is reported in that form.
            is_404 = isinstance(exc, HTTPError) and exc.code == HTTPStatus.NOT_FOUND
            message = _USP_NOT_FOUND if is_404 else str(refusal)
            return WebClientErrorResponse(message, retryable=retryable)

    def _fetch(self, url: str) -> AbstractWebClientResponse:
        """Open *url* through the guarded opener and gate the final URL."""
        request = urllib.request.Request(  # noqa: S310 — scheme + SSRF gated
            UrlAdmission.fetch_form(url), headers={"User-Agent": _USER_AGENT}
        )
        with GUARDED_OPENER.open(request, timeout=self._timeout) as resp:
            final = UrlAdmission.reject_reason(resp.url)
            if final is not None:
                return self._blocked(f"SSRF-blocked final URL: {final}", resp.url)
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
    def _is_transient(exc: Exception) -> bool:
        """Return whether USP may retry after *exc*: 5xx, 429, a timeout, no route."""
        if isinstance(exc, HTTPError):
            return exc.code >= 500 or exc.code == 429
        return isinstance(exc, TimeoutError | URLError)

    @staticmethod
    def _blocked(reason: str, url: str) -> WebClientErrorResponse:
        """Refuse *url* for a policy *reason*: never retried.

        Our layer logs it once; USP also reports the failed request.
        """
        refusal = FetchRefusedError.policy(reason, subject=UrlAdmission.shown(url))
        return WebClientErrorResponse(str(refusal), retryable=False)
