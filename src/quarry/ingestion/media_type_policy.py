"""The allow-list of media types worth fetching as text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, final

from quarry.content_guard import UnindexableContentError


class UnsupportedContentError(UnindexableContentError):
    """Raised before the body is read when the media type is not indexable text.

    The message carries no URL, so ``"content rejected:"`` is safe to log and to
    return to a client verbatim.
    """


@final
@dataclass(frozen=True, slots=True)
class MediaTypePolicy:
    """The allow-list of declared media types worth fetching as text.

    Anything else (a PDF, an image, an archive) is refused from the headers alone,
    before a single body byte is read.  An absent header is allowed: the content
    classifier still judges the decoded body.
    """

    exact: frozenset[str] = frozenset(
        {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
            "application/javascript",
        }
    )
    prefixes: tuple[str, ...] = ("text/",)
    suffixes: tuple[str, ...] = ("+json", "+xml")

    _UNSAFE: ClassVar[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9/.+;=_-]")
    _MAX_LEN: ClassVar[int] = 128
    _FALLBACK: ClassVar[str] = "application/octet-stream"

    def allows(self, media_type: str) -> bool:
        """Return whether *media_type* (lower-cased, parameters removed) is allowed."""
        return (
            not media_type
            or media_type in self.exact
            or media_type.startswith(self.prefixes)
            or media_type.endswith(self.suffixes)
        )

    def require(self, media_type: str) -> None:
        """Raise :class:`UnsupportedContentError` unless *media_type* is allowed."""
        if not self.allows(media_type):
            msg = (
                f"content rejected: unsupported media type {self.sanitize(media_type)}"
            )
            raise UnsupportedContentError(msg)

    @staticmethod
    def sanitize(raw: str) -> str:
        """Return a log- and marker-safe media type; fall back on unsafe input.

        ``raw`` is the ``Content-Type`` primary token from the HTTP response and
        is server-controlled.  Interpolating it verbatim into the
        ``<!-- media_type: X -->`` capture marker would let a header carrying
        whitespace, control bytes, or the ``-->`` needle break the single-line
        HTML-comment contract or escape the marker entirely.  Strip to the
        RFC 6838 shape (alphanumerics plus ``/.+;=_-``), cap at 128 chars, and
        substitute ``application/octet-stream`` when the input degrades to
        nothing after cleaning — the marker survives; the injection does not.
        """
        cleaned = MediaTypePolicy._UNSAFE.sub("", raw)[: MediaTypePolicy._MAX_LEN]
        if not cleaned or "-->" in cleaned:
            return MediaTypePolicy._FALLBACK
        return cleaned
