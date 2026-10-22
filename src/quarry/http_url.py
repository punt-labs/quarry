"""An admitted HTTP(S) URL that prints without its secrets."""

from __future__ import annotations

import re
from typing import ClassVar, Self, final
from urllib.parse import urlunsplit

from quarry.remote import to_netloc
from quarry.url_syntax import UrlSyntax

_MALFORMED = "malformed URL"


@final
class HttpUrl:
    """A URL parsed and admitted once, so a raw one cannot reach a sink by accident.

    The constructor applies :class:`UrlSyntax` and requires an ``http`` or ``https``
    scheme and a host, and raises ``ValueError("malformed URL")`` otherwise, with
    fixed text.  After that the value carries the only copies of the URL:

    * :attr:`fetch_url` is the one accessor that yields the fetchable form, and
      only a fetch or a third-party parser should ever call it.
    * :attr:`document_name` is the storage form, ``scheme://host[:port]/path``:
      userinfo, query, fragment and ``;params`` are dropped, the path stays
      because it names the document.
    * ``str`` and ``repr`` are the log form: the storage form with each
      high-entropy path segment (a token, a hash, a UUID) elided, since a secret
      can live in the path.

    A leak is therefore a ``fetch_url`` call you can grep for, not a regex miss.
    """

    __slots__ = ("_document_name", "_fetch_url", "_log_form")

    _fetch_url: str
    _document_name: str
    _log_form: str

    _TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_\-]{16,}")
    _PARAMS: ClassVar[re.Pattern[str]] = re.compile(r";[^/]*")

    def __new__(cls, raw: str) -> Self:
        parts = UrlSyntax.parse(raw)
        host = parts.hostname
        if parts.scheme.lower() not in {"http", "https"} or not host:
            raise ValueError(_MALFORMED)
        netloc = to_netloc(host, parts.port)
        self = super().__new__(cls)
        self._fetch_url = raw
        path = cls._PARAMS.sub("", parts.path)
        self._document_name = urlunsplit((parts.scheme, netloc, path, "", ""))
        self._log_form = urlunsplit((parts.scheme, netloc, cls._elide(path), "", ""))
        return self

    @property
    def fetch_url(self) -> str:
        """Return the admitted raw URL, the one form a fetch or a parser may use."""
        return self._fetch_url

    @property
    def document_name(self) -> str:
        """Return the storage form: ``scheme://host[:port]/path``."""
        return self._document_name

    @property
    def homepage(self) -> Self:
        """Return the site root, built from the host and port, never the raw netloc."""
        parts = UrlSyntax.parse(self._fetch_url)
        netloc = to_netloc(parts.hostname or "", parts.port)
        return type(self)(urlunsplit((parts.scheme, netloc, "/", "", "")))

    def __str__(self) -> str:
        return self._log_form

    def __repr__(self) -> str:
        return f"HttpUrl({self._log_form!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HttpUrl):
            return NotImplemented
        return self._fetch_url == other._fetch_url

    def __hash__(self) -> int:
        return hash((HttpUrl, self._fetch_url))

    @classmethod
    def _elide(cls, path: str) -> str:
        """Return *path* with each token-like segment elided."""
        return "/".join(cls._segment(segment) for segment in path.split("/"))

    @classmethod
    def _segment(cls, segment: str) -> str:
        """Return *segment*, or ``[…]`` when it looks like a token, hash or UUID."""
        is_token = cls._TOKEN.fullmatch(segment) is not None
        mixed = any(c.isdigit() for c in segment) and any(c.isalpha() for c in segment)
        return "[…]" if is_token and mixed else segment
