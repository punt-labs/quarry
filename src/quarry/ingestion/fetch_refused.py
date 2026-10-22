"""The URL-free refusal every fetch failure becomes."""

from __future__ import annotations

import contextlib
import logging
from typing import Self
from urllib.error import HTTPError, URLError

from quarry.content_guard import UnindexableContentError
from quarry.failure_reason import FailureReason
from quarry.ingestion.ssrf_redirect import RedirectRejectedError
from quarry.url_safety import UrlRejectedError

logger = logging.getLogger(__name__)


class FetchRefusedError(UnindexableContentError):
    """Raised when a fetch is refused or fails; the message is safe to log verbatim.

    Network libraries quote the raw URL (userinfo, path, query tokens) in their
    messages, so the fetch layer replaces every such failure with one of these:
    the reason names a rule or a kind of failure and never embeds userinfo, path
    or query (a host name may appear), so it is safe to log, to store as a task
    error, and to return to a client verbatim.
    """

    @classmethod
    def from_transfer_failure(cls, exc: Exception) -> Self:
        """Return the URL-free refusal for a failure while opening or reading.

        *exc* is an :class:`OSError` (including :class:`URLError` and
        :class:`HTTPError`), a :class:`ValueError`, or an
        :class:`http.client.HTTPException` (``InvalidURL``, ``BadStatusLine``,
        ``IncompleteRead``): the last carries the URL path and query verbatim.
        The SSRF policy's own rejections (a redirect hop, or the connect-time
        re-check after admission passed) name a rule, which an operator needs;
        every other exception reports only what kind of failure it was, and its
        message-free traceback is logged at WARNING, because a programming error
        must stay diagnosable and a DEBUG record is never written.  An
        :class:`HTTPError` holds an open socket, so it is closed here, before the
        status is read out.
        """
        if isinstance(exc, HTTPError):
            return cls(f"fetch failed: HTTP {cls._close(exc)}")
        if isinstance(exc, RedirectRejectedError):
            return cls(str(exc))
        if isinstance(exc, UrlRejectedError):
            return cls(f"URL rejected: {exc}")
        return cls(cls._describe(exc))

    @staticmethod
    def _close(exc: HTTPError) -> int:
        """Close the socket an :class:`HTTPError` holds and return its status code."""
        code = exc.code
        with contextlib.suppress(OSError, ValueError):
            exc.close()
        return code

    @staticmethod
    def _describe(exc: Exception) -> str:
        """Return the fixed reason for a failure that is neither a status nor a rule."""
        reason = exc.reason if isinstance(exc, URLError) else None
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            return "fetch failed: timed out"
        if isinstance(exc, URLError):
            return f"fetch failed: cannot reach the host ({type(reason).__name__})"
        logger.warning(
            "unclassified fetch failure %s:\n%s",
            type(exc).__name__,
            FailureReason.frames(exc),
        )
        return f"fetch failed: {type(exc).__name__}"
