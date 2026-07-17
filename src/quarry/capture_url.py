"""PII-safe metadata form of a WebFetch-captured URL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from quarry.remote import to_netloc


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
        """Return the metadata URL: userinfo/query/fragment stripped, then scrubbed.

        ``to_netloc`` re-brackets an IPv6 literal that ``urlsplit(...).hostname``
        stripped (``2001:db8::1`` -> ``[2001:db8::1]``), so the reassembled netloc
        is a valid, unambiguous URL — one shared bracketing primitive, no local copy.
        """
        parts = urlsplit(self._raw)
        netloc = to_netloc(parts.hostname or "", parts.port)
        bare = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        return scrubber(bare)
