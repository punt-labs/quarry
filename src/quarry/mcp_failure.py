"""How an MCP tool failure is logged and rendered for the agent that called it."""

from __future__ import annotations

import logging
from typing import Self, final

from quarry.client import HttpError, QuarryConnectionError

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
class ToolFailure:
    """One tool failure, logged and rendered identically on every MCP surface.

    A 4xx from the daemon is an expected refusal (bad input, unknown name, binary
    content): its message is the whole story, so the guard logs it as a WARNING
    without a traceback and it renders as ``Error: <message>``, the message being the
    same text the CLI and HTTP surfaces carry.  Anything else is a fault and keeps
    its traceback; a 5xx ``HttpError`` renders the same way but logs at ERROR
    with its traceback.
    """

    __slots__ = ("_exc",)

    _exc: Exception

    def __new__(cls, exc: Exception) -> Self:
        self = super().__new__(cls)
        self._exc = exc
        return self

    @property
    def level(self) -> int:
        """Return the log level: WARNING for a daemon refusal (a 4xx), else ERROR."""
        refusal = isinstance(self._exc, HttpError) and self._exc.status < 500
        return logging.WARNING if refusal else logging.ERROR

    @property
    def exc_info(self) -> Exception | None:
        """Return the exception to log a traceback for, or ``None`` for a refusal."""
        return None if self.level == logging.WARNING else self._exc

    def render(self) -> str:
        """Return the error string the tool returns instead of raising.

        A down daemon (``QuarryConnectionError``) renders WHAT failed plus the
        concrete recovery action (restart hint or remote-target hint) instead
        of a bare exception string — an agent reading the tool result can act
        on it without a separate ``quarry doctor`` round trip. A daemon
        rejection (``HttpError`` — e.g. a 400 on binary content) renders
        ``Error: <message>`` (a 5xx renders the same way but logs at ERROR with its
        traceback); any other exception renders ``Error: <class>: <message>``.
        One line, not two: a registered tool's result is a pydantic-repr'd
        ``TextContent``, which escapes an embedded newline — a two-line
        message would fail the direct-call/registered-call string-identity
        boundary (``tests/test_mcp_missions.py``).
        """
        exc = self._exc
        if isinstance(exc, QuarryConnectionError):
            hint = _RESTART_HINT if exc.is_loopback else _REMOTE_HINT
            return f"Error: {exc.message} — {hint}"
        if isinstance(exc, HttpError):
            return f"Error: {exc.message}"
        return f"Error: {type(exc).__name__}: {exc}"
