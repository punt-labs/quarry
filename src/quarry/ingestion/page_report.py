"""How one crawled page is named and how its failure is described in a log or report."""

from __future__ import annotations

import logging
import traceback
from http.client import HTTPException
from typing import Self, final

from quarry.capture_url import CaptureUrl
from quarry.content_guard import UnindexableContentError
from quarry.db.row_budget import RowBudgetError

logger = logging.getLogger(__name__)


@final
class PageReport:
    """A crawled page's redacted name, and the safe account of why it failed.

    The page URL is program-discovered and may carry a userinfo or query secret,
    so every progress, log and error sink sees only :attr:`url`, the redacted form
    (CWE-532).  A failure is described by its reason when it is an
    :class:`UnindexableContentError` (documented URL-free: a refusal or a failed
    fetch) and otherwise by its class name alone, because other messages quote the
    raw URL.  An expected failure (a refusal, a transport or protocol error, a
    row-budget violation) is a WARNING; anything else is a programming error and
    is logged at ERROR with a message-free traceback so it stays diagnosable
    without the URL.
    """

    __slots__ = ("_url",)

    _url: str

    def __new__(cls, page_url: str) -> Self:
        self = super().__new__(cls)
        self._url = CaptureUrl.display(page_url)
        return self

    @property
    def url(self) -> str:
        """Return the redacted page URL."""
        return self._url

    def failure(self, exc: Exception) -> str:
        """Log *exc* for this page and return its ``"<url>: <reason>"`` line."""
        reason = (
            str(exc) if isinstance(exc, UnindexableContentError) else type(exc).__name__
        )
        if isinstance(
            exc, UnindexableContentError | OSError | HTTPException | RowBudgetError
        ):
            logger.warning("Failed to ingest %s (%s)", self._url, reason)
        else:
            logger.error(
                "Failed to ingest %s (%s):\n%s",
                self._url,
                reason,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
        return f"{self._url}: {reason}"

    @staticmethod
    def require_progress(ingested: int, failed: int, errors: list[str]) -> None:
        """Raise if a crawl indexed nothing because every page failed.

        Raises:
            UnindexableContentError: ``ingested == 0`` and ``failed > 0``.
        """
        if ingested == 0 and failed > 0:
            msg = f"crawl ingested nothing: {failed} pages failed (first: {errors[0]})"
            raise UnindexableContentError(msg)
