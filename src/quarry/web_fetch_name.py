"""The document name a WebFetch capture is stored under."""

from __future__ import annotations

from typing import ClassVar, final

from quarry.capture_url import CaptureUrl


@final
class WebFetchName:
    """The one derivation of a WebFetch capture's document name from its URL.

    Both the capture write path (``hooks.handle_post_web_fetch``) and the lookup
    read path (``CaptureRoutes.lookup``) call :meth:`of`, so a URL always maps to
    the same document name regardless of which side computes it.  The scrub-log
    label is stamped on both sides for the same reason: one constant keeps the
    two derivations from drifting into different names for what should be the
    same stored capture.
    """

    __slots__ = ()

    _LABEL: ClassVar[str] = "web-fetch"

    @classmethod
    def of(cls, url: str) -> str:
        """Return *url*'s document name: userinfo, query and fragment dropped."""
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        return CaptureUrl(url).redacted(lambda text: scrub_and_log(text, cls._LABEL))
