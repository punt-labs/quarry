"""A fetched HTTP body and the gate every fetch passes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol, final

from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.media_type_policy import MediaTypePolicy
from quarry.url_admission import UrlAdmission


class UrlChecker(Protocol):
    """The URL admission gate (syntax, scheme, address) a fetch gate delegates to."""

    def reject_reason(self, url: str) -> str | None:
        """Return ``None`` if *url* is safe to fetch, else why not."""
        ...


@final
@dataclass(frozen=True, slots=True)
class FetchGate:
    """The checks a fetch passes: before the socket, after redirects, before the body.

    Every rejection is a :class:`FetchRefusedError` (or, for a media type, its
    :class:`UnsupportedContentError` sibling) naming a rule, never the URL.
    """

    policy: MediaTypePolicy = field(default_factory=MediaTypePolicy)
    urls: UrlChecker = UrlAdmission

    def check_url(self, url: str) -> None:
        """Raise unless *url* is HTTP(S) and resolves to a public address.

        Self-gates the initial URL (host + resolved address) BEFORE a socket is
        opened — defence in depth behind the route boundary, so a direct caller
        cannot reach an internal address.  Redirect hops are gated by the guarded
        opener; this covers the first hop.
        """
        if not url.lower().startswith(("http://", "https://")):
            msg = "URL rejected: only HTTP(S) URLs are supported"
            raise FetchRefusedError(msg)
        reason = self.urls.reject_reason(url)
        if reason is not None:
            msg = f"URL rejected: {reason}"
            raise FetchRefusedError(msg)

    def check_final_url(self, url: str) -> None:
        """Raise if the final URL (post-redirect) is unsafe/internal.

        Admission re-checks syntax, scheme and address, but only the address
        policy can newly fail after a redirect: the opener only follows HTTP(S)
        hops, and the syntax was admitted on the initial URL.
        """
        reason = self.urls.reject_reason(url)
        if reason is not None:
            msg = f"final URL rejected: {reason}"
            raise FetchRefusedError(msg)

    def check_media_type(self, media_type: str) -> None:
        """Raise :class:`UnsupportedContentError` unless the media type is text."""
        self.policy.require(media_type)


@final
@dataclass(frozen=True, slots=True)
class FetchedBody:
    """The decoded body of an HTTP fetch plus its declared media type.

    ``media_type`` is normalized on construction to the lower-cased
    ``Content-Type`` primary token (before any parameters) — ``"text/html"``,
    ``"application/json"``, ``"text/plain"``, or ``""`` when the response omitted
    the header.  Callers decide how to route the body: HTML through the
    extractor, everything else through the text pipeline.
    """

    text: str
    media_type: str

    _HTML: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "media_type", MediaTypePolicy.normalize(self.media_type)
        )

    @property
    def label(self) -> str:
        """Return the media type as safe to show in a message."""
        return MediaTypePolicy.label(self.media_type)

    def html_text(self) -> str:
        """Return the text of an HTML body, or raise a URL-free refusal.

        Raises:
            FetchRefusedError: The body is another media type.
        """
        if not self.is_html:
            msg = f"content rejected: non-HTML content {self.label}"
            raise FetchRefusedError(msg)
        return self.text

    @property
    def is_html(self) -> bool:
        """Return True when the body is HTML/XHTML or Content-Type was absent.

        A missing ``Content-Type`` is treated as HTML for backward
        compatibility with servers that omit the header — the contract of
        :meth:`WebFetcher.fetch`.
        """
        return not self.media_type or self.media_type in self._HTML
