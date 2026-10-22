"""PII-safe metadata form of a WebFetch-captured URL."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from quarry.remote import to_netloc

# Shown for a URL that cannot be parsed, never its raw text (where secrets sit).
_UNPARSEABLE = "(unparseable URL)"
_PARAMS = re.compile(r";[^/]*")  # path parameters: ";jsessionid=..." on any segment


@dataclass(frozen=True, slots=True)
class CaptureUrl:
    """A fetched URL whose persisted-metadata form carries no structural PII.

    A WebFetch capture stores its source URL as the document name and path.  A
    raw URL leaks anything in its userinfo, query, or fragment — the tokens and
    emails in ``…/reset?email=a@b.com&token=xyz`` — into the pushable
    web-captures collection even after the page body is scrubbed.  ``redacted``
    drops those structural parts and runs ``scheme://host/path`` through the
    same text scrubber for defence in depth, so only the bare location survives.

    Because query and fragment are dropped, two URLs differing only in those
    parts collapse to the SAME metadata form (and the same stored document
    name) — a trailing-slash difference in the path is NOT normalized and
    stays a distinct document.
    """

    _raw: str

    @classmethod
    def display(cls, raw: str) -> str:
        """Return *raw* redacted for a log or progress line; never raises.

        A URL that cannot be parsed (an out-of-range port, a malformed netloc)
        shows as a fixed placeholder rather than as its raw text.
        """
        with suppress(ValueError):
            return cls(raw).redacted(lambda text: text)
        return _UNPARSEABLE

    def redacted(self, scrubber: Callable[[str], str]) -> str:
        """Return the metadata URL: userinfo, query, fragment and params stripped.

        ``to_netloc`` re-brackets an IPv6 literal that ``urlsplit(...).hostname``
        stripped (``2001:db8::1`` -> ``[2001:db8::1]``), so the reassembled netloc
        is a valid, unambiguous URL — one shared bracketing primitive, no local copy.
        """
        parts = urlsplit(self._raw)
        netloc = to_netloc(parts.hostname or "", parts.port)
        path = _PARAMS.sub("", parts.path)
        return scrubber(urlunsplit((parts.scheme, netloc, path, "", "")))
