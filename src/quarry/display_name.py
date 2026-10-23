"""A document name as it may appear in a log, a progress line or the store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, final

from quarry.capture_url import CaptureUrl
from quarry.http_url import HttpUrl


@final
@dataclass(frozen=True, slots=True)
class DisplayName:
    """A document name with any URL in it stripped of what must not be kept.

    The name IS the URL for a plain URL ingest, and a ``remember`` may name a
    document with one, so a userinfo or query secret would otherwise reach the
    log, or every chunk's metadata, verbatim (CWE-532).  But the ingest funnel is
    also reached with an arbitrary caller-supplied name that is never a URL, and
    :class:`CaptureUrl` is unsafe on non-URL input: it silently truncates at the
    first ``#`` or ``?`` (both valid in a plain name).  So only an ``http(s)://``
    name is rewritten; any other name is shown verbatim, untruncated.  The prefix
    test ignores leading whitespace and control characters, as ``urlsplit`` does,
    so a padded URL cannot slip past it.  An ``http(s)://`` name that cannot be
    parsed shows as a fixed placeholder, never as its raw text.
    """

    name: str

    _LEADING: ClassVar[str] = "".join(chr(code) for code in range(0x21))

    def stored(self) -> str:
        """Return the storage form: a URL's userinfo, query and fragment dropped."""
        text = self.name.lstrip(self._LEADING)
        if not text.lower().startswith(("http://", "https://")):
            return self.name
        try:
            return HttpUrl(text).document_name
        except ValueError:
            return CaptureUrl.display(text)  # tolerant of userinfo; never shows it

    def redacted(self) -> str:
        """Return the log form: ``scheme://host[:port]/…(segments=N)``, no path."""
        text = self.name.lstrip(self._LEADING)
        if not text.lower().startswith(("http://", "https://")):
            return self.name
        return HttpUrl.shown(text)
