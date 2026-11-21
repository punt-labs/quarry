"""A document name as it may appear in a log, a progress line or the store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from quarry.document_url import DocumentUrl


@final
@dataclass(frozen=True, slots=True)
class DisplayName:
    """A document name with any URL in it stripped of what must not be kept.

    The name IS the URL for a plain URL ingest, and a ``remember`` may name a
    document with one, so a userinfo or query secret would otherwise reach the
    log, or every chunk's metadata, verbatim (CWE-532).  But the ingest funnel is
    also reached with an arbitrary caller-supplied name that is never a URL, and
    :class:`CaptureUrl` is unsafe on non-URL input: it silently truncates at the
    first ``#`` or ``?`` (both valid in a plain name).  Plain names are
    kept intact in the store, but WebSearch names carry queries and must not
    be displayed in diagnostics. :class:`DocumentUrl` handles both admitted
    and malformed HTTP(S) names without exposing the raw URL.
    """

    name: str

    def stored(self, *, url_sourced: bool) -> str:
        """Strip URL credentials and fragments and retain only a query digest.

        A *url_sourced* name is a page's, so a name this build wrote reads back as
        itself.  A label is not: it is cleaned like any URL, but its query is never
        read as a digest already, so typing the marker cannot name another page.
        """
        url = DocumentUrl.from_name(self.name)
        return self.name if url is None else url.stored(url_sourced=url_sourced)

    def redacted(self) -> str:
        """Return the diagnostic name without WebSearch queries or URL paths."""
        if self.name.startswith("search: "):
            return "WebSearch capture"
        url = DocumentUrl.from_name(self.name)
        return self.name if url is None else url.redacted()
