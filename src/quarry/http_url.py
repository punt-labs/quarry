"""An admitted HTTP(S) URL that prints without its secrets."""

from __future__ import annotations

import re
from typing import ClassVar, Self, final
from urllib.parse import urlsplit, urlunsplit

from quarry.capture_url import CaptureUrl
from quarry.remote import to_netloc
from quarry.url_syntax import UrlSyntax

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
    * :attr:`document_name` is the storage form, ``scheme://host[:port]/path``:
      userinfo, query, fragment and ``;params`` are dropped, the path stays
      because it names the document.
    * ``str`` and ``repr`` are the log form, ``scheme://host[:port]/…(N segments)``:
      never any of the path, because a secret can live in it and no heuristic
      recognizes every token (a JWT, padded base64, a short code).

    The host is normalized so equivalent URLs share a name: lower-cased, the
    ideographic full stops IDNA reads as ``.`` replaced, a trailing dot dropped,
    and the scheme's default port omitted.

    A leak is therefore a ``fetch_url`` call you can grep for, not a regex miss.
    """

    __slots__ = ("_document_name", "_fetch_url", "_host", "_log_form", "_path")

    _fetch_url: str
    _document_name: str
    _host: str
    _log_form: str
    _path: str

    _PARAMS: ClassVar[re.Pattern[str]] = re.compile(r";[^/]*")
    # IDNA reads these as the label separator; so do we, or "example\u3002com" and
    # "example.com" would be two documents.
    _STOPS: ClassVar[dict[int, str]] = dict.fromkeys((0x3002, 0xFF0E, 0xFF61), ".")
    _DEFAULT_PORTS: ClassVar[dict[str, int]] = {"http": 80, "https": 443}

    def __new__(cls, raw: str) -> Self:
        parts = UrlSyntax.parse(raw)
        host = (parts.hostname or "").translate(cls._STOPS).rstrip(".")
        if parts.scheme not in cls._DEFAULT_PORTS or not host:
            raise ValueError(_MALFORMED)
        port = None if parts.port == cls._DEFAULT_PORTS[parts.scheme] else parts.port
        netloc = to_netloc(host, port)
        path = cls._PARAMS.sub("", parts.path)
        self = super().__new__(cls)
        self._fetch_url = raw
        self._host = host
        self._path = path
        self._document_name = urlunsplit((parts.scheme, netloc, path or "/", "", ""))
        self._log_form = cls._form(parts.scheme, netloc, path)
        return self

    @classmethod
    def shown(cls, raw: str) -> str:
        """Return *raw* as it may appear in a log: the one producer of that text.

        An admitted URL shows its log form; one :class:`HttpUrl` refuses shows
        the same shape through the tolerant :class:`CaptureUrl` reading.  Never
        raises, and never any of the path.
        """
        try:
            return str(cls(raw))
        except ValueError:
            pass
        tolerant = urlsplit(CaptureUrl.display(raw))
        host = tolerant.hostname or ""
        if tolerant.scheme not in cls._DEFAULT_PORTS or not 0 < len(host) <= 253:
            return _UNPARSEABLE
        return cls._form(tolerant.scheme, tolerant.netloc, tolerant.path)

    @staticmethod
    def _form(scheme: str, netloc: str, path: str) -> str:
        """Return ``scheme://netloc/…(N segments)``, the shape of a URL in a log."""
        count = len([part for part in path.split("/") if part])
        return f"{scheme}://{netloc}/…({count} segment{'s' * (count != 1)})"

    @property
    def fetch_url(self) -> str:
        """Return the admitted raw URL, the one form a fetch or a parser may use."""
        return self._fetch_url

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
