"""Remember the first bug a fetch hit, by class, for a caller that swallows it."""

from __future__ import annotations

import logging
from typing import Self, final

from quarry.failure_reason import FailureReason

logger = logging.getLogger(__name__)


@final
class FetchBugRecord:
    """The class name of the first bug seen since the last :meth:`take`.

    A library that catches every exception a fetch raises (USP does, for each
    sub-sitemap) turns a bug into an ordinary-looking failed document.  The fetch
    layer, which sees the exception first, records it here and logs it once, with
    its class and message-free frames, and a caller reads :meth:`take` afterwards.
    """

    __slots__ = ("_first",)

    _first: str

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._first = ""
        return self

    def record(self, exc: Exception, subject: str) -> None:
        """Log *exc* against the redacted *subject* and remember its class."""
        self._first = self._first or type(exc).__name__
        logger.error(
            "fetch hit a bug [%s]: %s\n%s",
            subject,
            type(exc).__name__,
            FailureReason.frames(exc),
        )

    def take(self) -> str:
        """Return the recorded class name, or ``""``, and forget it."""
        first, self._first = self._first, ""
        return first
