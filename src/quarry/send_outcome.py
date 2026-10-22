"""The result of one send to the daemon: whether it landed, and how a failure reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

_UNREACHABLE = "daemon-unreachable"
_REFUSED = "daemon-refused"
_ERROR = "daemon-error"


@final
@dataclass(frozen=True, slots=True)
class SendOutcome:
    """Whether a send reached the daemon and, if not, how to say so.

    Truthy when the daemon took the request.  A failure carries the log line and
    the trace label.  Named distinctly so an operator is not misled: a connection
    failure is "unreachable" (what that costs differs per caller, so the caller
    supplies its text); a 4xx means the daemon is up and refused the request; a
    local misconfiguration, a 5xx or a malformed reply is a plain error.  It
    imports nothing from the client: a hook that never sends must not pay for it.
    """

    sent: bool
    label: str = ""
    log: str = ""

    def __bool__(self) -> bool:
        return self.sent

    @classmethod
    def unreachable(cls, log: str) -> Self:
        """Return the outcome of a connection failure, with the caller's *log*."""
        return cls(sent=False, label=_UNREACHABLE, log=log)

    @classmethod
    def error(cls, log: str) -> Self:
        """Return the outcome of a local, malformed or server-side failure."""
        return cls(sent=False, label=_ERROR, log=log)

    @classmethod
    def rejected(cls, status: int, message: str) -> Self:
        """Return the outcome of a non-2xx reply: a 4xx is a refusal."""
        label = _REFUSED if 400 <= status < 500 else _ERROR
        log = f"daemon rejected request: HTTP {status} — {message}"
        return cls(sent=False, label=label, log=log)
