"""A fetched HTTP body and the media-type policy that decides what is fetched."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar, final

from quarry.content_guard import UnsupportedContentError
from quarry.url_safety import UrlSafetyCheck


@final
@dataclass(frozen=True, slots=True)
class MediaTypePolicy:
    """The allow-list of declared media types worth fetching as text.

    Anything else (a PDF, an image, an archive) is refused from the headers alone,
    before a single body byte is read.  An absent header is allowed: the content
    classifier still judges the decoded body.
    """

    exact: frozenset[str] = frozenset(
        {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
            "application/javascript",
        }
    )
    prefixes: tuple[str, ...] = ("text/",)
    suffixes: tuple[str, ...] = ("+json", "+xml")

    _UNSAFE: ClassVar[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9/.+;=_-]")
    _MAX_LEN: ClassVar[int] = 128
    _FALLBACK: ClassVar[str] = "application/octet-stream"

    def allows(self, media_type: str) -> bool:
        """Return whether *media_type* (lower-cased, parameters removed) is allowed."""
        return (
            not media_type
            or media_type in self.exact
            or media_type.startswith(self.prefixes)
            or media_type.endswith(self.suffixes)
        )

    def require(self, media_type: str) -> None:
        """Raise :class:`UnsupportedContentError` unless *media_type* is allowed."""
        if not self.allows(media_type):
            msg = (
                f"content rejected: unsupported media type {self.sanitize(media_type)}"
            )
            raise UnsupportedContentError(msg)

    @staticmethod
    def sanitize(raw: str) -> str:
        """Return a log- and marker-safe media type; fall back on unsafe input.

        ``raw`` is the ``Content-Type`` primary token from the HTTP response and
        is server-controlled.  Interpolating it verbatim into the
        ``<!-- media_type: X -->`` capture marker would let a header carrying
        whitespace, control bytes, or the ``-->`` needle break the single-line
        HTML-comment contract or escape the marker entirely.  Strip to the
        RFC 6838 shape (alphanumerics plus ``/.+;=_-``), cap at 128 chars, and
        substitute ``application/octet-stream`` when the input degrades to
        nothing after cleaning — the marker survives; the injection does not.
        """
        cleaned = MediaTypePolicy._UNSAFE.sub("", raw)[: MediaTypePolicy._MAX_LEN]
        if not cleaned or "-->" in cleaned:
            return MediaTypePolicy._FALLBACK
        return cleaned


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
