"""The result of one send to the daemon: whether it landed, and how a failure reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self, final

_UNREACHABLE = "daemon-unreachable"
_REFUSED = "daemon-refused"
_ERROR = "daemon-error"
_CONFIG = "config-error"
_BACKFILL = (
    "run 'quarry backfill-sessions', "
    "which indexes missed sessions of registered directories"
)


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

    # label -> (what went wrong and where to look, what to fix first, what
    # recovers the session once it is fixed).  The recovery is a conditional that
    # is true for any session: backfill indexes the missed sessions of registered
    # directories, and says nothing about whether this one is.
    _ADVICE: ClassVar[dict[str, tuple[str, str, str]]] = {
        _UNREACHABLE: (
            "quarryd is not reachable, so this session was not indexed now",
            "",
            _BACKFILL,
        ),
        _REFUSED: ("the daemon refused the capture; see quarry.log", "", ""),
        _CONFIG: (
            "the daemon target is misconfigured",
            "check QUARRY_URL / quarry login",
            _BACKFILL,
        ),
        _ERROR: (
            "the daemon failed; see quarryd.log or quarry doctor",
            "",
            f"{_BACKFILL} once it recovers",
        ),
    }

    def __bool__(self) -> bool:
        return self.sent

    @classmethod
    def advice(cls, label: str, *, archived: bool, written: bool) -> str:
        """Return the warning for a capture whose send failed with *label*.

        The one mapping from a failure to what the user is told.  The local copies
        are described on their own and promise only what landed: *archived* is
        whether the raw transcript was archived and *written* whether the scrubbed
        capture file was.  Backfill reads Claude Code's own transcripts, not that
        archive, so it is offered for every failure it can recover, archived or
        not.  A daemon that refused the capture will refuse a backfill too.
        """
        what, fix, recovery = cls._ADVICE.get(label, cls._ADVICE[_ERROR])
        if archived:
            copies = (
                "the transcript and the scrubbed capture were"
                if written
                else ("the transcript was")
            )
            kept = f"A durable copy of {copies} saved"
        else:
            kept = "The session transcript could not be archived; see quarry.log"
            kept += ". The scrubbed capture was saved" if written else ""
        steps = [fix, recovery]
        remedy = "; then ".join(step for step in steps if step)
        return f"Warning: {what}. {kept}{'; ' + remedy if remedy else ''}."

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
    def config(cls, log: str) -> Self:
        """Return the outcome of a hook-side misconfiguration (QUARRY_URL, login)."""
        return cls(sent=False, label=_CONFIG, log=log)

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
