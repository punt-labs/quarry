"""The MCP tool-boundary decorator every tool module shares."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@final
class ToolGuard:
    """The one error boundary between a tool body and the stdio transport.

    A namespace class rather than a free function (PY-OO-7): ``McpTools`` and
    every sibling tool module decorate with the same ``ToolGuard.wrap`` without
    either importing the other, so a tool defined anywhere shares an identical
    boundary. Stateless by construction — ``__slots__ = ()`` and one
    ``@staticmethod``.
    """

    __slots__ = ()

    @staticmethod
    def wrap(method: Callable[..., str]) -> Callable[..., str]:
        """Wrap a tool method so any failure returns an error string at the boundary.

        A down daemon (``QuarryConnectionError``), a daemon rejection
        (``HttpError`` — e.g. a 404 on an unknown collection), or any escape is
        logged and rendered as ``Error: …`` rather than propagating; the stdio
        transport never sees a raised exception, and there is no in-process
        engine fallback. Applied at definition so a direct call and the
        registered tool share the identical boundary.
        """

        @functools.wraps(method)
        def wrapper(*args: object, **kwargs: object) -> str:
            try:
                return method(*args, **kwargs)
            # The MCP tool-handler boundary: any failure becomes a returned string.
            except Exception as exc:
                logger.exception("Error in %s", method.__name__)
                return f"Error: {type(exc).__name__}: {exc}"

        return wrapper
