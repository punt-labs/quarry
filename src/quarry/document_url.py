"""Recognize and render document names that are HTTP(S) URLs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self, final

from quarry.capture_url import CaptureUrl
from quarry.http_url import HttpUrl


@final
@dataclass(frozen=True, slots=True)
class DocumentUrl:
    """A document name with a URL prefix, even if the URL is malformed."""

    _text: str

    _LEADING: ClassVar[str] = "".join(chr(code) for code in range(0x21))

    @classmethod
    def from_name(cls, name: str) -> Self | None:
        """Recognize a URL prefix after the same leading characters urlsplit ignores."""
        text = name.lstrip(cls._LEADING)
        return cls(text) if text.lower().startswith(("http://", "https://")) else None

    def stored(self, *, url_sourced: bool) -> str:
        """Return the storage URL, retaining existing digests only for pages."""
        try:
            if url_sourced:
                return HttpUrl.stored(self._text)
            return HttpUrl(self._text).document_name
        except ValueError:
            return CaptureUrl.display(self._text)

    def redacted(self) -> str:
        """Return the diagnostic form without any path or credentials."""
        return HttpUrl.shown(self._text)
