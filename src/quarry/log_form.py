"""The one shape a URL takes in a log: host and port, never any of the path."""

from __future__ import annotations

from typing import final


@final
class LogForm:
    """Render ``scheme://host[:port]/…(N segments)``.

    A secret can live in any path segment and no heuristic recognizes every token
    (a JWT, padded base64, a short code), so a log shows how many segments a path
    had and nothing of what they said.
    """

    __slots__ = ()

    @staticmethod
    def of(scheme: str, netloc: str, path: str) -> str:
        """Return the log form of a URL given its scheme, netloc and path."""
        count = len([part for part in path.split("/") if part])
        return f"{scheme}://{netloc}/…({count} segment{'s' * (count != 1)})"
