"""PII-safe metadata form of a WebFetch-captured URL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class CaptureUrl:
    """A fetched URL whose persisted-metadata form carries no structural PII.

    A WebFetch capture stores its source URL as the document name and path.  A
    raw URL leaks anything in its userinfo, query, or fragment — the tokens and
    emails in ``…/reset?email=a@b.com&token=xyz`` — into the pushable
    web-captures collection even after the page body is scrubbed.  ``redacted``
    drops those structural parts and runs ``scheme://host/path`` through the
    same text scrubber for defence in depth, so only the bare location survives.
    """

    _raw: str

    def redacted(self, scrubber: Callable[[str], str]) -> str:
        """Return the metadata URL: userinfo/query/fragment stripped, then scrubbed."""
        parts = urlsplit(self._raw)
        host = self._bracketed(parts.hostname or "")
        netloc = f"{host}:{parts.port}" if parts.port else host
        bare = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        return scrubber(bare)

    @staticmethod
    def _bracketed(host: str) -> str:
        """Wrap an IPv6 literal in ``[]`` so its colons aren't read as a port.

        ``urlsplit(...).hostname`` strips the RFC 3986 brackets from IPv6 hosts,
        so ``[2001:db8::1]`` returns as ``2001:db8::1``.  Reassembling a netloc
        from the bare literal yields an ambiguous, invalid URL; restore the
        brackets whenever the host carries the tell-tale colon.
        """
        return f"[{host}]" if ":" in host else host
