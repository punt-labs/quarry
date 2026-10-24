"""Say whether a redirect's ``Location`` header cannot be parsed."""

from __future__ import annotations

from typing import TYPE_CHECKING, final
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from http.client import HTTPMessage


@final
class RedirectLocation:
    """The ``Location`` of a 30x response, checked the way CPython will read it.

    CPython joins the target to the request URL before the redirect gate runs, and
    a malformed one (``http://[``, ``//[x/``, a host that does not survive NFKC)
    raises a bare ``ValueError`` there.  That is a refusal of this hop, not a bug,
    so the same parse is made first, where it can be told apart from a fault.
    """

    __slots__ = ()

    @staticmethod
    def is_malformed(request_url: str, headers: HTTPMessage) -> bool:
        """Return whether the redirect target cannot be parsed."""
        location = headers.get("location") or headers.get("uri")
        if location is None:
            return False
        try:
            urlparse(location)
            urljoin(request_url, location)
        except ValueError:
            return True
        return False
