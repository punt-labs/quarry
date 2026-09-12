"""PII-safe metadata form of a WebFetch-captured URL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from quarry.remote import to_netloc

# The scrub-log label every WebFetch capture URL is stamped with, at both the
# write (hooks.handle_post_web_fetch) and read (captures/lookup route) sides —
# a single constant keeps the two derivations from drifting into different
# document names for what should be the same stored capture.
_WEB_FETCH_LABEL = "web-fetch"


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
    def for_web_fetch(cls, raw: str) -> str:
        """Return *raw*'s WebFetch document-name form: the one true derivation.

        Both the capture write path (``hooks.handle_post_web_fetch``) and the
        lookup read path (``CaptureRoutes.lookup``) call this so a URL always
        maps to the same document name regardless of which side computes it.
        """
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        return cls(raw).redacted(lambda text: scrub_and_log(text, _WEB_FETCH_LABEL))

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
