"""Remember the first bug a fetch hit, by class, for a caller that swallows it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# The common fault classes of a pure string redactor.  A blind ``Exception`` is not
# allowed, so a fault outside this list is not caught here and would replace the
# bug being recorded.
_FAULTS = (
    ArithmeticError,
    AssertionError,
    AttributeError,
    ImportError,
    LookupError,
    NameError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


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

    def record(self, exc: Exception, url: str, shown: Callable[[str], str]) -> None:
        """Log *exc* against *url* as *shown* redacts it, and remember its class.

        The class is remembered first.  If *shown* raises one of the common fault
        classes of a string redactor, the subject names that class instead.
        """
        self._first = self._first or type(exc).__name__
        try:
            subject = shown(url)
        except _FAULTS as fault:  # a redactor fault must not replace the bug
            subject = f"(url not shown: {type(fault).__name__})"
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
