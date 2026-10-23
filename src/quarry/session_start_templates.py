"""Every SessionStart ``additionalContext`` string, in one place."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.shown_value import Shown

# Two canonical pointer sentences, spliced verbatim into the SessionStart
# ``additionalContext`` and the MCP server ``instructions`` block. The deep
# how-to — trigger clusters, scoping, failure recovery, the five moments, the
# agent_handle discipline — lives in the quarry-recall/quarry-capture skills;
# these sentences exist so every surface points at the same two skills instead
# of each maintaining its own paraphrase of the same guidance (the drift the
# design set out to prevent).
_TRIGGER_RULES = (
    "Understanding code, a project decision, or a memory? See the quarry-recall skill.",
    "Remembering, learning, or ingesting something? See the quarry-capture skill.",
)


@final
class SessionStartTemplates:
    """Produce the SessionStart ``additionalContext`` strings that are not counts.

    Owns the skill-pointer trailer and the per-branch templates so ``hooks``'s
    dispatcher never assembles ad-hoc strings.  Each ``@classmethod`` maps to
    exactly one dispatcher branch; the shared trailer is stitched into the
    trigger-carrying branches at one place.  A *directory* or *collection*
    arrives as a :class:`~quarry.shown_value.Shown`: whoever made the directory
    chose its path, and the model that reads this text must not take it for
    instructions or a command.
    """

    __slots__ = ()

    @classmethod
    def trailer(cls) -> str:
        """Return the skill-pointer rules joined by newlines."""
        return "\n".join(_TRIGGER_RULES)

    @classmethod
    def subsumption(cls, directory: Shown) -> str:
        """Child registrations exist under this directory — trailer still applies.

        A subsumption refusal says nothing about the daemon; ``find``/``remember``
        against the covering child still work, so the trigger rules are emitted.
        """
        header = (
            f"Quarry: child registrations exist under {directory.text}. "
            "Auto-register skipped to prevent subsumption. "
            f"To register the parent run: quarry enable {directory.arg}"
        )
        return f"{header}\n{cls.trailer()}"

    @classmethod
    def daemon_unreachable(cls, directory: Shown) -> str:
        """Auto-register deferred because quarryd is unreachable.

        Per operator ratification R2b: the trailer is emitted even here — the
        agent is not a passive receiver; it can restart quarryd, run
        ``quarry doctor``, and then apply the rules once the tools come back.
        """
        header = (
            "Quarry is enabled for this repo but quarryd is currently unreachable.\n"
            "Once you restart it (systemctl --user restart quarry / "
            "launchctl kickstart) the tools below become available.\n"
            f"Auto-registration of {directory.text} is deferred until quarryd returns."
        )
        return f"{header}\n{cls.trailer()}"

    @classmethod
    def nudge_enable(cls, directory: Shown) -> str:
        """No marker, no coverage: nudge the operator to run ``quarry enable``."""
        return (
            "Quarry semantic search is available but not enabled for this project.\n"
            f"Directory: {directory.text}\n"
            "This directory is not registered for sync. To turn quarry on:\n"
            f"  quarry enable {directory.arg}\n"
            "This runs once, commits an opt-in marker, deposits the agent guide,\n"
            "and registers this directory for background sync."
        )

    @classmethod
    def drift_surface(cls, directory: Shown, collection: Shown) -> str:
        """Marker absent + coverage exists: surface the drift, no auto-fix."""
        return (
            "Quarry: this project has an indexed collection but no opt-in marker\n"
            f"({directory.text}, collection {collection.text}). Two doors:\n"
            f"  quarry enable {directory.arg}         re-adopt: marker + guide.\n"
            f"  quarry deregister {collection.arg}    "
            "drop the registration (keep-data).\n"
            "Auto-register is refused (already registered); auto-deregister is\n"
            "refused (would delete indexed data on marker drift)."
        )
