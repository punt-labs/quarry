"""A document name as it may appear in a log or progress line."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from quarry.capture_url import CaptureUrl


@final
@dataclass(frozen=True, slots=True)
class DisplayName:
    """A document name redacted for a log or progress line, never for storage.

    The name IS the URL for a plain (unscrubbed) URL ingest, so a userinfo or
    query secret would otherwise reach the log verbatim (CWE-532).  But the
    ingest funnel is also reached with an arbitrary caller-supplied name that is
    never a URL, and :class:`CaptureUrl` is unsafe on non-URL input: it silently
    truncates at the first ``#`` or ``?`` (both valid in a plain name), and some
    malformed strings resembling a URL authority raise ``ValueError`` out of
    ``urlsplit``.  So only an ``http(s)://`` name is redacted, and any other name,
    or a redaction that still fails, is shown verbatim, untruncated.
    """

    name: str

    def redacted(self) -> str:
        """Return the name with a URL's userinfo, query and fragment dropped."""
        if not self.name.lower().startswith(("http://", "https://")):
            return self.name
        try:
            return CaptureUrl(self.name).redacted(lambda text: text)
        except ValueError:
            return self.name
