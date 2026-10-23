"""What a SessionStart hook answers with, and how its trace names it."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry._hook_trace import HookTrace, Outcome
from quarry.refusal import RegistrationRefusedError
from quarry.session_start_templates import SessionStartTemplates
from quarry.shown_value import Shown

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.sync_registry import SyncRegistry

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class SessionStartReply:
    """A SessionStart envelope, and how the hook's trace names what produced it.

    ``kind`` is the trace outcome: a refusal or a deferral is not a capture, so
    the line in ``quarry.log`` says so.
    """

    envelope: dict[str, object]
    kind: Outcome = "capture"
    detail: str = ""

    @classmethod
    def of(cls, context: str, kind: Outcome = "capture", detail: str = "") -> Self:
        """Return the reply that wraps *context* in the hook-response envelope."""
        envelope: dict[str, object] = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        }
        return cls(envelope, kind, detail)

    @classmethod
    def nudge_enable(cls, directory: Path) -> Self:
        """Return the reply that nudges the operator to ``quarry enable`` here."""
        return cls.of(
            SessionStartTemplates.nudge_enable(Shown.of(directory)),
            "skip",
            "not-enabled",
        )

    @classmethod
    def drift_surface(cls, directory: Path, collection: str) -> Self:
        """Return the reply that surfaces a registration with no opt-in marker."""
        return cls.of(
            SessionStartTemplates.drift_surface(
                Shown.of(directory), Shown.of(collection)
            ),
            "skip",
            "marker-drift",
        )

    @classmethod
    def subsumption(cls, directory: Path) -> Self:
        """Return the skip reply for a directory with child registrations under it."""
        return cls.of(
            SessionStartTemplates.subsumption(Shown.of(directory)),
            "skip",
            "subsumption",
        )

    @classmethod
    def daemon_unreachable(cls, directory: Path) -> Self:
        """Return the error reply for an auto-register deferred on a down daemon."""
        return cls.of(
            SessionStartTemplates.daemon_unreachable(Shown.of(directory)),
            "error",
            "daemon-unreachable",
        )

    @classmethod
    def refusing(
        cls, registry: SyncRegistry, directory: Path, collection: str
    ) -> Self | None:
        """Register *directory*; return the reply that states a refusal, else ``None``.

        The refusal's text is fixed and names no path or collection, so it is safe
        to show; its detail (which collection, which directory) goes to the redacted
        log line.  A refusal says nothing about the daemon, so the trailer still
        applies.
        """
        try:
            registry.register_directory(directory, collection)
        except RegistrationRefusedError as exc:
            logger.warning(
                "session-start: auto-register of %s refused: %s [%s]",
                directory,
                exc,
                exc.detail,
            )
            then = (
                "to register under another collection name, run "
                "'quarry enable -c <name>'"
                if exc.renamable
                else "run 'quarry enable' after resolving it"
            )
            header = (
                f"Quarry: auto-register of this directory was refused: {exc}.\n"
                f"Nothing was registered; {then}."
            )
            context = f"{header}\n{SessionStartTemplates.trailer()}"
            return cls.of(context, "skip", "registration-refused")
        return None

    def traced(self, trace: HookTrace) -> dict[str, object]:
        """Emit the trace line for this reply and return the envelope."""
        if self.kind == "skip":
            trace.skip(self.detail)
        elif self.kind == "error":
            trace.error(self.detail)
        else:
            trace.capture(self.detail)
        return self.envelope
