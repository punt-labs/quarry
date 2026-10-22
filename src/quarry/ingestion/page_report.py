"""How one crawled page is named and how its failure is described in a log or report."""

from __future__ import annotations

from typing import Self, final

from quarry.capture_url import CaptureUrl
from quarry.content_guard import UnindexableContentError


@final
class PageReport:
    """A crawled page's redacted name and the safe rendering of why it failed.

    The page URL is program-discovered and may carry a userinfo or query secret,
    so every progress, log and error sink sees only :attr:`url`, the redacted form
    (CWE-532).  A failure is described by its reason when it is an
    :class:`UnindexableContentError` (documented URL-free: a refusal or a failed
    fetch) and otherwise by its class name alone, because other exceptions quote
    the raw URL.
    """

    __slots__ = ("_url",)

    _url: str

    def __new__(cls, page_url: str) -> Self:
        self = super().__new__(cls)
        self._url = CaptureUrl(page_url).redacted(lambda text: text)
        return self

    @property
    def url(self) -> str:
        """Return the redacted page URL."""
        return self._url

    @staticmethod
    def failure(exc: Exception) -> str:
        """Return what is safe to say about *exc*."""
        if isinstance(exc, UnindexableContentError):
            return str(exc)
        return type(exc).__name__
