"""A fetched HTTP body and the gate every fetch passes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, final

from quarry.ingestion.media_type_policy import MediaTypePolicy
from quarry.url_safety import UrlSafetyCheck


@final
@dataclass(frozen=True, slots=True)
class FetchGate:
    """The checks a fetch passes: before the socket, after redirects, before the body.

    Each rejection is a ``ValueError`` whose message names a rule, never the URL, so
    it is safe to log.
    """

    policy: MediaTypePolicy = field(default_factory=MediaTypePolicy)

    @staticmethod
    def check_url(url: str) -> None:
        """Raise unless *url* is HTTP(S) and resolves to a public address.

        Self-gates the initial URL (host + resolved address) BEFORE a socket is
        opened — defence in depth behind the route boundary, so a direct caller
        cannot reach an internal address.  Redirect hops are gated by the guarded
        opener; this covers the first hop.
        """
        if not url.lower().startswith(("http://", "https://")):
            msg = f"Only HTTP(S) URLs are supported: {url}"
            raise ValueError(msg)
        reason = UrlSafetyCheck.reject_reason(url)
        if reason is not None:
            msg = f"URL rejected: {reason}"
            raise ValueError(msg)

    @staticmethod
    def check_final_url(url: str) -> None:
        """Raise if the final URL (post-redirect) is unsafe/internal."""
        reason = UrlSafetyCheck.reject_reason(url)
        if reason is not None:
            msg = f"final URL rejected: {reason}"
            raise ValueError(msg)

    def check_media_type(self, media_type: str) -> None:
        """Raise :class:`UnsupportedContentError` unless the media type is text."""
        self.policy.require(media_type)


@final
@dataclass(frozen=True, slots=True)
class FetchedBody:
    """The decoded body of an HTTP fetch plus its declared media type.

    ``media_type`` is the lower-cased ``Content-Type`` primary token
    (before any parameters) — ``"text/html"``, ``"application/json"``,
    ``"text/plain"``, or ``""`` when the response omitted the header.
    Callers decide how to route the body: HTML through the extractor,
    everything else through the text pipeline (G4 capture-as-text).
    """

    text: str
    media_type: str

    _HTML: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})

    @property
    def is_html(self) -> bool:
        """Return True when the body is HTML/XHTML or Content-Type was absent.

        A missing ``Content-Type`` is treated as HTML for backward
        compatibility with servers that omit the header — the pre-G4
        contract for :meth:`WebFetcher.fetch`.
        """
        return not self.media_type or self.media_type in self._HTML
