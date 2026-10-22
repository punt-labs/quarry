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
    classifier still judges the decoded body.  Entries are lowercase and a media
    type is normalized (parameters dropped, lowercased) before it is compared.
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

    _TOKEN: ClassVar[re.Pattern[str]] = re.compile(
        r"[a-z0-9][a-z0-9.+_-]{0,126}/[a-z0-9.+_-]{1,127}"
    )

    def __post_init__(self) -> None:
        for entry in (*self.exact, *self.prefixes, *self.suffixes):
            if entry != entry.lower():
                msg = f"media type policy entries must be lowercase, got {entry!r}"
                raise ValueError(msg)

    @staticmethod
    def normalize(raw: str) -> str:
        """Return the lower-cased primary token of a ``Content-Type`` header."""
        return raw.split(";", 1)[0].strip().lower()

    def allows(self, media_type: str) -> bool:
        """Return whether the (header-form) *media_type* is allowed."""
        token = self.normalize(media_type)
        return (
            not token
            or token in self.exact
            or token.startswith(self.prefixes)
            or token.endswith(self.suffixes)
        )

    def require(self, media_type: str) -> None:
        """Raise :class:`UnsupportedContentError` unless *media_type* is allowed."""
        if not self.allows(media_type):
            label = self.label(media_type)
            msg = f"content rejected: unsupported media type {label}"
            raise UnsupportedContentError(msg)

    @classmethod
    def label(cls, media_type: str) -> str:
        """Return *media_type* for a message, or ``(invalid)`` if it is not a token.

        The header is server-controlled, so only a well-formed ``type/subtype``
        is echoed into a log line or an error.
        """
        token = cls.normalize(media_type)
        return token if cls._TOKEN.fullmatch(token) else "(invalid)"
