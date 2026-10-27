"""An admitted HTTP(S) URL that prints without its secrets."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import ClassVar, Self, final
from urllib.parse import quote, urlsplit, urlunsplit

from quarry.capture_url import CaptureUrl
from quarry.log_form import LogForm
from quarry.remote import to_netloc
from quarry.url_syntax import UrlSyntax

logger = logging.getLogger(__name__)

_MALFORMED = "malformed URL"
_UNPARSEABLE = "(unparseable URL)"


@final
class HttpUrl:
    """A URL parsed and admitted once, so a raw one cannot reach a sink by accident.

    The constructor applies :class:`UrlSyntax` and requires an ``http`` or ``https``
    scheme and a host, and raises ``ValueError("malformed URL")`` otherwise, with
    fixed text.  After that the value carries the only copies of the URL:

    * :attr:`fetch_url` is the one accessor that yields the fetchable form, and
      only a fetch or a third-party parser should ever call it.
    * :attr:`document_name` is the storage form, ``scheme://host[:port]/path``,
      with ``?q[sha256]=<digest>`` after it when there is a query.  Userinfo,
      fragment and ``;params`` are dropped; the path stays because it names the
      document; the query is kept only as a digest of its sorted, encoded
      parameters, so ``?page=1`` and ``?page=2`` stay two documents.  The raw
      query never reaches the store; a low-entropy query is guessable from its
      digest.
    * ``str`` and ``repr`` are the log form, ``scheme://host[:port]/…(segments=N)``:
      never any of the path, because a secret can live in it and no heuristic
      recognizes every token (a JWT, padded base64, a short code).

    The host is normalized so equivalent URLs share a name: lower-cased, the
    ideographic full stops IDNA reads as ``.`` replaced, a trailing dot dropped,
    and the scheme's default port omitted.

    A leak is therefore a ``fetch_url`` call you can grep for, not a regex miss.
    """

    __slots__ = (
        "_document_name",
        "_fetch_url",
        "_homepage",
        "_host",
        "_log_form",
        "_path",
    )

    _fetch_url: str
    _document_name: str
    _homepage: Self
    _host: str
    _log_form: str
    _path: str

    _PARAMS: ClassVar[re.Pattern[str]] = re.compile(r";[^/]*")
    # IDNA reads these as the label separator; so do we, or "example\u3002com" and
    # "example.com" would be two documents.
    _STOPS: ClassVar[dict[int, str]] = dict.fromkeys((0x3002, 0xFF0E, 0xFF61), ".")
    _DEFAULT_PORTS: ClassVar[dict[str, int]] = {"http": 80, "https": 443}
    # The query of a stored name.  A real query cannot hold it: ``[`` and ``]`` are
    # percent-encoded there, so a URL can never produce this marker by itself.
    _DIGEST: ClassVar[re.Pattern[str]] = re.compile(r"q\[sha256\]=[0-9a-f]{16}")

    def __new__(cls, raw: str) -> Self:
        parts = UrlSyntax.parse(raw)
        host = (parts.hostname or "").translate(cls._STOPS).rstrip(".")
        if parts.scheme not in cls._DEFAULT_PORTS or not host:
            raise ValueError(_MALFORMED)
        port = None if parts.port == cls._DEFAULT_PORTS[parts.scheme] else parts.port
        netloc = to_netloc(host, port)
        encoded = tuple(
            cls._encoded(part) for part in (parts.path, parts.query, parts.fragment)
        )
        path = cls._PARAMS.sub("", encoded[0])
        # An ASCII URL is fetched exactly as given; otherwise the encoded form.
        fetch = (
            raw
            if encoded == (parts.path, parts.query, parts.fragment)
            else (urlunsplit((parts.scheme, parts.netloc, *encoded)))
        )
        root = urlunsplit(
            (parts.scheme, to_netloc(parts.hostname or "", parts.port), "/", "", "")
        )
        self = super().__new__(cls)
        fields = {
            "_fetch_url": fetch,
            "_host": host,
            "_path": path,
            "_document_name": urlunsplit(
                (parts.scheme, netloc, path or "/", cls._query_key(encoded[1]), "")
            ),
            "_log_form": LogForm.of(parts.scheme, netloc, path),
        }
        for name, value in fields.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_homepage", self if root == fetch else cls(root))
        return self

    def __setattr__(self, name: str, value: object) -> None:
        msg = "HttpUrl is immutable"
        raise AttributeError(msg)

    def __delattr__(self, name: str) -> None:
        msg = "HttpUrl is immutable"
        raise AttributeError(msg)

    @staticmethod
    def _encoded(part: str) -> str:
        """Return a path, query or fragment percent-encoded as RFC 3986 asks.

        Non-ASCII text becomes UTF-8 escapes; so do the ASCII characters RFC 3986
        does not allow in a path or query (space-free here, but ``[ ] | " < > { } ^``
        and the backtick); a stray ``%`` becomes ``%25``; and an existing escape is
        kept with its hex digits upper-cased.  So ``café`` and ``caf%c3%a9`` are one
        document and nothing non-ASCII reaches the request.
        """
        text = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", part)
        escaped = quote(text, safe="/?:@!$&'()*+,;=-._~%")
        return re.sub(r"%[0-9a-f]{2}", lambda m: m.group().upper(), escaped)

    @classmethod
    def _query_key(cls, query: str) -> str:
        """Return the stored form of an encoded *query*: the marker, or ``""``.

        The digest is the first 16 hex digits of the SHA-256 of the query's
        non-empty parameters, sorted, so parameter order does not make a second
        document.
        """
        normalized = "&".join(sorted(part for part in query.split("&") if part))
        if not normalized:
            return ""
        return f"q[sha256]={hashlib.sha256(normalized.encode()).hexdigest()[:16]}"

    @classmethod
    def stored(cls, name: str) -> str:
        """Return the storage form of *name*, which may already be one.

        A real URL is canonicalized (:attr:`document_name`).  A name that already
        ends in the digest marker keeps its digest and has the rest canonicalized
        again, so reading a stored name back gives the same name.

        Only for names read back from the store, or built by this class: a URL a
        user or a page supplied goes through ``HttpUrl(raw)``, which never
        recognises the marker.  ``LegacyUrlNames`` is the only caller; a test pins
        that.

        Raises:
            ValueError: *name* is not an admissible HTTP(S) URL.
        """
        parts = urlsplit(name)
        if not cls._DIGEST.fullmatch(parts.query):
            return cls(name).document_name
        bare = cls(urlunsplit(parts._replace(query="", fragment=""))).document_name
        return f"{bare}?{parts.query}"

    @classmethod
    def shown(cls, raw: str) -> str:
        """Return *raw* as it may appear in a log: the one producer of that text.

        An admitted URL shows its log form; one :class:`HttpUrl` refuses shows
        the same shape through the tolerant :class:`CaptureUrl` reading.  Never
        raises, and never any of the path.
        """
        try:
            return str(cls(raw))
        except ValueError as exc:
            logger.debug("HttpUrl refused a URL: %s", type(exc).__name__)
        tolerant = urlsplit(CaptureUrl.display(raw))
        host = tolerant.hostname or ""
        if tolerant.scheme not in cls._DEFAULT_PORTS or not 0 < len(host) <= 253:
            return _UNPARSEABLE
        return LogForm.of(tolerant.scheme, tolerant.netloc, tolerant.path)

    @property
    def fetch_url(self) -> str:
        """Return the admitted raw URL, the one form a fetch or a parser may use."""
        return self._fetch_url

    @property
    def has_query_or_params(self) -> bool:
        """Return whether the URL carries a query or ``;params`` (a fragment is not)."""
        parts = urlsplit(self._fetch_url)
        return bool(parts.query) or ";" in parts.path

    @property
    def host(self) -> str:
        """Return the lower-cased host, without brackets or port."""
        return self._host

    @property
    def path(self) -> str:
        """Return the path with ``;params`` removed (``""`` for a bare host)."""
        return self._path

    @property
    def document_name(self) -> str:
        """Return the storage form: ``scheme://host[:port]/path[?q[sha256]=<digest>]``."""
        return self._document_name

    @property
    def homepage(self) -> Self:
        """Return the site root, built from the host and port, never the raw netloc."""
        return self._homepage

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
