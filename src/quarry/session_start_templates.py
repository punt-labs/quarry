"""Every SessionStart ``additionalContext`` string, in one place."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.results import CoverageCounts


# Three canonical trigger sentences — used verbatim in the SessionStart
# ``additionalContext``, the MCP server ``instructions`` block, and the recall
# skill.  Any drift between surfaces is what the design set out to prevent, so
# the sentences live in a single module constant and are spliced by reference,
# not paraphrase.
_TRIGGER_RULES = (
    "Use find before WebSearch or WebFetch for research, or before "
    "answering a why/how/what-did-we-decide question.",
    "Prefer grep for symbol and value lookups; prefer find for meaning.",
    "Use remember when you learn something durable — a decision, a gotcha, "
    "a non-obvious fact, a procedure — so it survives context compaction.",
)


@final
class SessionStartTemplates:
    """Produce every SessionStart ``additionalContext`` string.

    Owns the three-rule trailer and the per-branch templates so ``hooks``'s
    dispatcher never assembles ad-hoc strings.  Each ``@classmethod`` maps to
    exactly one dispatcher branch; the shared trailer is stitched into the
    trigger-carrying branches at one place.
    """

    __slots__ = ()

    _SLASH_TAIL = (
        "Slash commands: /find, /ingest, /remember, /explain, /source, /quarry. "
        "For deep research across local docs and the web, use the researcher agent."
    )

    @classmethod
    def _trailer(cls) -> str:
        """Return the R1/R2/R3 rules joined by newlines."""
        return "\n".join(_TRIGGER_RULES)

    @classmethod
    def _sync_line(cls, status: str) -> str:
        """Render the launched/running/failed background-sync status line."""
        return {
            "launched": "Background sync in progress.",
            "running": "Background sync already running.",
        }.get(status, "Background sync failed to launch.")

    @classmethod
    def active(
        cls,
        directory: Path,
        collection: str,
        captures_collection: str,
        counts: CoverageCounts,
        sync_status: str,
    ) -> str:
        """Reachable-coverage active-mode context: counts + rules + sync + slash."""
        header = (
            f"Quarry semantic search is active for this project.\n"
            f'Collection: "{collection}" ({directory})\n'
            f'Captures: "{captures_collection}"\n'
            f"{counts['documents_indexed']} documents indexed, "
            f"{counts['transcripts_captured']} transcripts captured, "
            f"{counts['memories_saved']} memories saved."
        )
        return (
            f"{header}\n{cls._trailer()}\n"
            f"{cls._sync_line(sync_status)}\n{cls._SLASH_TAIL}"
        )

    @classmethod
    def active_unreachable_coverage(
        cls,
        directory: Path,
        collection: str,
        captures_collection: str,
        sync_status: str,
    ) -> str:
        """Active-mode context when the coverage query itself failed.

        Registration and background sync are local operations that can succeed
        while the daemon's HTTP API refuses the coverage call — the daemon is
        unreachable, an HTTP status refused (401 not-authorized, 5xx), or the
        client is misconfigured.  The trailer is still emitted so an agent that
        reads this message can act on the diagnosis and then apply the rules.
        """
        header = (
            f"Quarry semantic search is active for this project.\n"
            f'Collection: "{collection}" ({directory})\n'
            f'Captures: "{captures_collection}"\n'
            "Coverage counts unavailable "
            "(quarryd unreachable or client not authorized)."
        )
        return (
            f"{header}\n{cls._trailer()}\n"
            f"{cls._sync_line(sync_status)}\n{cls._SLASH_TAIL}"
        )

    @classmethod
    def subsumption(cls, directory: Path) -> str:
        """Child registrations exist under this directory — trailer still applies.

        A subsumption refusal says nothing about the daemon; ``find``/``remember``
        against the covering child still work, so the trigger rules are emitted.
        """
        header = (
            f"Quarry: child registrations exist under {directory}. "
            "Auto-register skipped to prevent subsumption. "
            f"Run 'quarry enable {directory}' to register the parent."
        )
        return f"{header}\n{cls._trailer()}"

    @classmethod
    def daemon_unreachable(cls, directory: Path) -> str:
        """Auto-register deferred because quarryd is unreachable.

        Per operator ratification R2b: the trailer is emitted even here — the
        agent is not a passive receiver; it can restart quarryd, run
        ``quarry doctor``, and then apply the rules once the tools come back.
        """
        header = (
            "Quarry is enabled for this repo but quarryd is currently unreachable.\n"
            "Once you restart it (systemctl --user restart quarry / "
            "launchctl kickstart) the tools below become available.\n"
            f"Auto-registration of {directory} is deferred until quarryd returns."
        )
        return f"{header}\n{cls._trailer()}"

    @classmethod
    def nudge_enable(cls, directory: Path) -> str:
        """No marker, no coverage: nudge the operator to run ``quarry enable``."""
        return (
            "Quarry semantic search is available but not enabled for this project.\n"
            f"Directory: {directory}\n"
            "This directory is not registered for sync. To turn quarry on:\n"
            f"  quarry enable {directory}\n"
            "This runs once, commits an opt-in marker, deposits the agent guide,\n"
            "and registers this directory for background sync."
        )

    @classmethod
    def drift_surface(cls, directory: Path, collection: str) -> str:
        """Marker absent + coverage exists: surface the drift, no auto-fix."""
        return (
            "Quarry: this project has an indexed collection but no opt-in marker\n"
            f"({directory}, collection {collection!r}). Two doors:\n"
            f"  quarry enable {directory}         re-adopt: marker + guide.\n"
            f"  quarry deregister {collection}    drop the registration (keep-data).\n"
            "Auto-register is refused (already registered); auto-deregister is\n"
            "refused (would delete indexed data on marker drift)."
        )
