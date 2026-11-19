"""Render enable/disable results into human-readable CLI summary lines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.enable import DisableResult, EnableResult

__all__ = ["DisableReport", "EnableReport"]


def _flagged(pairs: tuple[tuple[object, str], ...]) -> list[str]:
    """Return each message whose flag is truthy, preserving order.

    Turns a run of ``if result.x: lines.append(...)`` branches into one
    data-driven pass, so a report carries content, not control flow.
    """
    return [message for flag, message in pairs if flag]


@final
class EnableReport:
    """Format an :class:`EnableResult` as the lines ``quarry enable`` prints."""

    __slots__ = ("_r",)

    _r: EnableResult

    def __new__(cls, result: EnableResult) -> Self:
        """Retain one enable outcome for consistent human-readable reporting."""
        self = super().__new__(cls)
        self._r = result
        return self

    def lines(self) -> list[str]:
        """Return the summary lines for the enable, in display order."""
        r = self._r
        lines = [
            f"Enabled quarry for {r.directory}",
            f"  Collection: {r.collection}",
            f"  Captures: {r.captures_collection}",
        ]
        lines += _flagged(
            (
                (r.config_path, f"  Config: {r.config_path}"),
                (
                    r.guide_deposited,
                    "  Deposited quarry guide to .punt-labs/quarry/CLAUDE.md",
                ),
                (
                    r.import_registered,
                    "  Registered @.punt-labs/quarry/CLAUDE.md in CLAUDE.md",
                ),
                (
                    r.enabled_marker_written,
                    "  Wrote enabled marker",
                ),
                (r.gitignore_ensured, "  .gitignore excludes captures and lock files"),
            )
        )
        lines += r.permissions.lines(
            "Added quarry permission rules to",
            unchanged="  Quarry permission rules already in .claude/settings.json",
        )
        lines += self._skills_lines()
        return lines + self._ethos_lines()

    def _skills_lines(self) -> list[str]:
        """Return the cross-harness skill-deposit line, or none.

        A tuple of :class:`~quarry.skills_install.SkillOutcome` is not a flag
        ``_flagged`` can branch on, so this names the harnesses directly.
        Only ``"deposited"`` and ``"upgraded"`` count as new content;
        ``"current"`` (unchanged on a re-enable) and ``"unsupported"``
        (Claude Code, served by the plugin directly) are not, and a re-enable
        where every skill is already current (or no source tree/non-Claude
        harness exists at all) omits the line entirely — the same "visible by
        its absence" convention :meth:`DisableReport.lines` uses for its own
        marker line. A ``dict``'s insertion-ordered keys are the
        de-duplicating, order-preserving set this needs — a plain ``set``
        would scramble the display order.
        """
        new_actions = ("deposited", "upgraded")
        harnesses = dict.fromkeys(
            o.harness.value for o in self._r.skills_deposited if o.action in new_actions
        )
        line = f"  Deposited agent skills to: {', '.join(harnesses)}"
        return [line] if harnesses else []

    def _ethos_lines(self) -> list[str]:
        """Return the ethos-identity portion of the summary.

        The vendored line can accompany "not installed": a repo's committed
        ext files are refreshed whether or not the operator has a global ethos
        install, and that refresh is a working-tree diff to commit — announced
        here so it is committed, not discovered. The failed handles are listed
        last: the guide never landed for them, so "Ethos created" must not read
        as unqualified success.
        """
        e = self._r.ethos
        return _flagged(
            (
                (e.skipped, "  Ethos: not installed (agent memory skipped)"),
                (e.created, f"  Ethos created: {', '.join(e.created)}"),
                (e.updated, f"  Ethos updated: {', '.join(e.updated)}"),
                (
                    e.memory_collections,
                    f"  Memory collections: {', '.join(e.memory_collections)}",
                ),
                (
                    e.vendored_updated,
                    "  Ethos guide refreshed (vendored — commit via PR): "
                    + ", ".join(e.vendored_updated),
                ),
                (e.failed, f"  Ethos FAILED: {', '.join(e.failed)}"),
            )
        )


@final
class DisableReport:
    """Format a :class:`DisableResult` as the lines ``quarry disable`` prints."""

    __slots__ = ("_keep_data", "_r")

    _r: DisableResult
    _keep_data: bool

    def __new__(cls, result: DisableResult, *, keep_data: bool) -> Self:
        """Pair a disable outcome with the caller's keep-data reporting policy."""
        self = super().__new__(cls)
        self._r = result
        self._keep_data = keep_data
        return self

    def lines(self) -> list[str]:
        """Return the summary lines for the disable, in display order."""
        r = self._r
        lines = [f"Disabled quarry for {r.directory}"]
        if r.collection:
            # Report the registration in both keep-data branches so --keep-data
            # does not look like only local files were touched.
            fate = "kept indexed data" if self._keep_data else "chunk purge queued"
            lines.append(f"  Deregistered {r.collection} ({r.removed} files); {fate}")
        else:
            # Idempotent no-op: nothing was registered (never enabled, or a prior
            # partial disable already deregistered it).
            lines.append("  Already disabled (no registration)")
        lines += _flagged(
            (
                (r.config_removed, "  Config file removed"),
                (
                    r.import_pruned,
                    "  Removed @.punt-labs/quarry/CLAUDE.md from CLAUDE.md",
                ),
                (
                    r.enabled_marker_removed,
                    "  Removed enabled marker (guide left dormant)",
                ),
            )
        )
        return lines + r.permissions.lines("Removed quarry tool permissions from")
