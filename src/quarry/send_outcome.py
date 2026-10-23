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
    def advice(cls, label: str, *, archived: bool, written: bool) -> str:
        """Return the warning for a capture whose send failed with *label*.

        The one mapping from a failure to what the user is told, and it promises
        only what landed: *archived* is whether the raw transcript was archived
        and *written* whether the scrubbed capture file was.  Only an unreachable
        daemon with an archive is told to backfill later; a daemon that said no,
        or failed, is told where to look.
        """
        if label == _UNREACHABLE:
            what = "quarryd is not reachable, so this session was not indexed now"
        elif label == _REFUSED:
            what = "the daemon refused the capture; see quarry.log"
        else:
            what = "the daemon failed; see quarry.log or quarry doctor"
        if not archived:
            lost = "the session transcript could not be archived; see quarry.log"
            kept = " The scrubbed capture is saved." if written else ""
            return f"Warning: {what}, and {lost}.{kept}"
        saved = "archive and scrubbed capture are" if written else "archive is"
        backfill = (
            "; run 'quarry backfill-sessions' to index them later"
            if label == _UNREACHABLE
            else ""
        )
        return f"Warning: {what}. The raw JSONL {saved} saved{backfill}."

    def named(self, done: str, attempt: str) -> str:
        """Return *done* when the send landed, else ``<attempt>-<label>``.

        How a trace names a step that follows a send: ``distilled`` or
        ``distill-daemon-refused``.
        """
        return done if self.sent else f"{attempt}-{self.label}"

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
