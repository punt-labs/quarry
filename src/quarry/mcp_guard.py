"""The MCP tool-boundary decorator every tool module shares."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, final

from quarry.client import QuarryConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Mirrors the CLI's autostart nudge (``__main__._AUTOSTART_HINT``) for the MCP
# surface. Shown only when the unreachable target is loopback (DES-031):
# a remote daemon's absence can never be fixed by restarting a local service,
# so the two branches carry different, non-misleading recovery actions.
_RESTART_HINT = (
    "Restart it: 'systemctl --user restart quarry' (Linux) or 'launchctl "
    "kickstart -k gui/$(id -u)/com.punt-labs.quarry' (macOS), then retry. "
    "No results were returned for this call."
)
_REMOTE_HINT = (
    "This is a remote target — restarting a local daemon will not help. "
    "Check QUARRY_URL / 'quarry login', then retry."
)


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

        Every failure is logged and rendered via :meth:`_render` rather than
        propagating; the stdio transport never sees a raised exception, and
        there is no in-process engine fallback. Applied at definition so a
        direct call and the registered tool share the identical boundary.
        """

        @functools.wraps(method)
        def wrapper(*args: object, **kwargs: object) -> str:
            try:
                return method(*args, **kwargs)
            # The MCP tool-handler boundary: any failure becomes a returned string.
            except Exception as exc:
                logger.exception("Error in %s", method.__name__)
                return ToolGuard._render(exc)

        return wrapper

    @staticmethod
    def _render(exc: Exception) -> str:
        """Return the boundary's error string for *exc*.

        A down daemon (``QuarryConnectionError``) renders WHAT failed plus the
        concrete recovery action (restart hint or remote-target hint) instead
        of a bare exception string — an agent reading the tool result can act
        on it without a separate ``quarry doctor`` round trip. A daemon
        rejection (``HttpError`` — e.g. a 404 on an unknown collection) or any
        other exception still renders the generic ``Error: …`` form. One
        line, not two: a registered tool's result is a pydantic-repr'd
        ``TextContent``, which escapes an embedded newline — a two-line
        message would fail the direct-call/registered-call string-identity
        boundary (``tests/test_mcp_missions.py``).
        """
        if isinstance(exc, QuarryConnectionError):
            hint = _RESTART_HINT if exc.is_loopback else _REMOTE_HINT
            return f"Error: {exc.message} — {hint}"
        return f"Error: {type(exc).__name__}: {exc}"
