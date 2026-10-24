"""How one crawled page is named and how its failure is described in a log or report."""

from __future__ import annotations

import logging
from http.client import HTTPException
from typing import TYPE_CHECKING, Self, final

from quarry.content_guard import UnindexableContentError
from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.http_url import HttpUrl

logger = logging.getLogger(__name__)


@final
class PageReport:
    """A crawled page's redacted name, and the safe account of why it failed.

    The page URL is program-discovered and may carry a userinfo or query secret,
    so every progress, log and error sink sees only :attr:`url`, the redacted form
    (CWE-532).  A failure is described by :class:`FailureReason`: its reason when
    it is a :class:`RefusalError` (documented URL-free: a refusal or a failed
    fetch) and otherwise ``internal error: <Class>``, because other messages quote
    the raw URL.  An expected failure (a refusal, a transport or protocol error, a
    row-budget violation) is a WARNING; anything else is a programming error and
    is logged at ERROR.  A refusal is one line; any other failure also logs its
    message-free frames, and an ``OSError`` its errno and strerror (never its
    filename), so a fault stays diagnosable without the URL.
    """

    __slots__ = ("_url",)

    _url: str

    def __new__(cls, page_url: HttpUrl) -> Self:
        self = super().__new__(cls)
        self._url = str(page_url)
        return self

    @property
    def url(self) -> str:
        """Return the redacted page URL."""
        return self._url

    def failure(self, exc: Exception) -> str:
        """Log *exc* for this page and return its ``"<url>: <reason>"`` line."""
        reason = FailureReason.of(exc)
        if isinstance(exc, RefusalError):
            logger.warning("Failed to ingest %s (%s)", self._url, reason)
        else:
            level = (
                logging.WARNING
                if isinstance(exc, OSError | HTTPException)
                else logging.ERROR
            )
            logger.log(
                level,
                "Failed to ingest %s (%s)%s:\n%s",
                self._url,
                reason,
                self._os_detail(exc),
                FailureReason.frames(exc),
            )
        return f"{self._url}: {reason}"

    @staticmethod
    def _os_detail(exc: Exception) -> str:
        """Return an :class:`OSError`'s errno and strerror, never its filename."""
        if isinstance(exc, OSError):
            return f" [errno={exc.errno} {exc.strerror}]"
        return ""

    @staticmethod
    def require_progress(ingested: int, failed: int, reasons: list[str]) -> None:
        """Raise if a crawl indexed nothing because every page failed.

        *reasons* are :meth:`FailureReason.of` values, so the task error names no URL.

        Raises:
            UnindexableContentError: ``ingested == 0`` and ``failed > 0``.
        """
        if ingested == 0 and failed > 0:
            msg = f"crawl ingested nothing: {failed} pages failed (first: {reasons[0]})"
            raise UnindexableContentError(msg)
