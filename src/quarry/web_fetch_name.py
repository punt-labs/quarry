"""The document name a WebFetch capture is stored under."""

from __future__ import annotations

from typing import ClassVar, final

from quarry.http_url import HttpUrl


@final
class WebFetchName:
    """The one derivation of a WebFetch capture's document name from its URL.

    Both the capture write path (``hooks.handle_post_web_fetch``) and the lookup
    read path (``CaptureRoutes.lookup``) call it, so a URL always maps to the
    same document name regardless of which side computes it: the storage form of
    the admitted URL, ``scheme://host[:port]/path[?q=<digest>]``, run through the
    scrubber.
    The scrub-log label is stamped on both sides for the same reason: one
    constant keeps the two derivations from drifting into different names for
    what should be the same stored capture.
    """

    __slots__ = ()

    _LABEL: ClassVar[str] = "web-fetch"

    @classmethod
    def of(cls, url: str) -> str:
        """Return *url*'s document name.

        Raises:
            ValueError: *url* is not an admissible HTTP(S) URL; the message is the
                fixed text ``malformed URL``.
        """
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        return scrub_and_log(HttpUrl(url).document_name, cls._LABEL)

    @staticmethod
    def shown(url: str) -> str:
        """Return *url* as it may appear in context or a log: host and segments only."""
        return HttpUrl.shown(url)

    @classmethod
    def maybe(cls, url: str) -> str | None:
        """Return :meth:`of`, or ``None`` for a URL that could never be stored."""
        try:
            return cls.of(url)
        except ValueError:
            return None
