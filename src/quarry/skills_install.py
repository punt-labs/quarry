"""Deposit quarry's canonical skills into every detected coding-agent harness.

The canonical content lives once, at ``<repo>/plugin/skills/<name>/`` (Claude
Code's marketplace plugin tree). This module copies that content, verbatim,
into the harness-specific locations that pi, opencode, and codex auto-load
skills from — Claude Code needs no deposit of its own, since it already reads
the canonical tree directly. Every deposit is version-stamped by content hash
so a re-run is a no-op unless the source changed. :class:`SkillSource`
(``skills_source.py``) owns the source-tree and single-deposit mechanics
(locate, hash, manifest, the atomic copy-and-swap); this module owns only
harness orchestration — which harness, which root, which skills go where.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Self, final

from quarry.skills_source import SkillSource

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["Harness", "SkillOutcome", "SkillsInstaller"]

# The closed vocabulary a deposit/status/removal call reports. "unsupported"
# is Claude Code only (no deposit target); "current"/"stale" are status-only;
# "deposited"/"upgraded" are install-only; "removed"/"absent"/"foreign" are
# removal-only ("foreign": a same-named directory exists but carries no
# quarry manifest, so it was refused, not deleted — see PY-EH-8/safety note
# on :meth:`SkillsInstaller._remove_one`).
Action = Literal[
    "deposited",
    "upgraded",
    "current",
    "stale",
    "removed",
    "absent",
    "unsupported",
    "foreign",
]


class Harness(StrEnum):
    """A coding-agent harness quarry can deposit its skills into.

    Each member knows its own deposit root and detection marker — confirmed
    against the installed harnesses, not guessed: pi's ``getAgentDir()``
    resolves to ``~/.pi/agent`` (its own qodo skill deposit lives at
    ``~/.pi/agent/skills/``); opencode's skill loader globs
    ``~/.config/opencode/{skill,skills}/<name>/SKILL.md`` as a "global
    skills" root; codex's own ``skill-installer`` system skill deposits
    user-installed skills at ``$CODEX_HOME/skills/<name>/`` (default
    ``~/.codex/skills``), reserving the sibling ``.system/`` directory for
    codex's own preinstalled skills — so quarry deposits alongside it, never
    inside it. Claude Code needs no deposit: it reads the two skills directly
    from the repo's ``plugin/skills/`` marketplace tree.
    """

    CLAUDE = "claude"
    PI = "pi"
    OPENCODE = "opencode"
    CODEX = "codex"

    def deposit_root(self, home: Path) -> Path | None:
        """Return where this harness auto-loads deposited skills from.

        ``None`` only for :attr:`CLAUDE` — the documented "nothing to
        deposit" contract, not a lookup failure.
        """
        if self is Harness.CLAUDE:
            return None
        if self is Harness.PI:
            return home / ".pi" / "agent" / "skills"
        if self is Harness.OPENCODE:
            return home / ".config" / "opencode" / "skills"
        return home / ".codex" / "skills"

    def detected(self, home: Path) -> bool:
        """Return whether this harness looks installed on *home*'s machine."""
        if self is Harness.CLAUDE:
            return (home / ".claude").is_dir()
        if self is Harness.PI:
            return (home / ".pi").is_dir()
        if self is Harness.OPENCODE:
            return (home / ".config" / "opencode").is_dir()
        return (home / ".codex").is_dir()


@final
@dataclass(frozen=True, slots=True)
class SkillOutcome:
    """One skill's deposit/removal/status result for one harness."""

    harness: Harness
    skill: str
    action: Action
    # None only when action == "unsupported" (Claude Code has no deposit path).
    path: Path | None

    def __post_init__(self) -> None:
        if (self.action == "unsupported") != (self.path is None):
            msg = "SkillOutcome.path must be set iff action is not 'unsupported'"
            raise ValueError(msg)


@final
class SkillsInstaller:
    """Deposit quarry's canonical skills into every targeted harness.

    Idempotent and version-stamped: each deposited skill directory carries a
    ``.quarry-skill.json`` manifest recording a content hash, so a re-run with
    unchanged source content is a no-op (``action="current"``) and a changed
    source overwrites the stale copy wholesale (``action="upgraded"``).
    """

    __slots__ = ("_home", "_source")

    _source: SkillSource
    _home: Path

    def __new__(cls, source_dir: Path, home: Path) -> Self:
        self = super().__new__(cls)
        self._source = SkillSource(source_dir)
        self._home = home
        return self

    @staticmethod
    def locate_source(start: Path) -> Path:
        """Walk upward from *start* to find the repo's ``plugin/skills`` tree.

        Delegates to :meth:`SkillSource.locate`; kept here too since this is
        the class every caller (the CLI, tests) already names.
        """
        return SkillSource.locate(start)

    def detected_harnesses(self) -> tuple[Harness, ...]:
        """Return every harness that looks installed on this machine."""
        return tuple(harness for harness in Harness if harness.detected(self._home))

    def install(self, harness: Harness) -> tuple[SkillOutcome, ...]:
        """Deposit every canonical skill into *harness*; idempotent."""
        root = harness.deposit_root(self._home)
        if root is None:
            return self._unsupported(harness)
        return tuple(
            self._install_one(harness, name, root)
            for name in self._source.skill_names()
        )

    def install_detected(self) -> tuple[SkillOutcome, ...]:
        """Deposit into every harness :meth:`detected_harnesses` finds."""
        outcomes: list[SkillOutcome] = []
        for harness in self.detected_harnesses():
            outcomes.extend(self.install(harness))
        return tuple(outcomes)

    def status(self) -> tuple[SkillOutcome, ...]:
        """Report every harness/skill's current state without writing anything."""
        outcomes: list[SkillOutcome] = []
        for harness in Harness:
            root = harness.deposit_root(self._home)
            if root is None:
                outcomes.extend(self._unsupported(harness))
                continue
            outcomes.extend(
                self._status_one(harness, name, root)
                for name in self._source.skill_names()
            )
        return tuple(outcomes)

    def remove(self, harness: Harness) -> tuple[SkillOutcome, ...]:
        """Delete every deposited skill for *harness*; idempotent.

        Refuses (``action="foreign"``) any same-named directory that lacks
        quarry's own manifest — a directory this installer never deposited
        is never a candidate for ``shutil.rmtree``, no matter how it got
        there (see the safety note on :meth:`_remove_one`).
        """
        root = harness.deposit_root(self._home)
        if root is None:
            return self._unsupported(harness)
        return tuple(
            self._remove_one(harness, name, root) for name in self._source.skill_names()
        )

    def remove_all(self) -> tuple[SkillOutcome, ...]:
        """Delete every deposited skill from every harness; idempotent."""
        outcomes: list[SkillOutcome] = []
        for harness in Harness:
            outcomes.extend(self.remove(harness))
        return tuple(outcomes)

    def _unsupported(self, harness: Harness) -> tuple[SkillOutcome, ...]:
        return tuple(
            SkillOutcome(harness, name, "unsupported", None)
            for name in self._source.skill_names()
        )

    def _install_one(self, harness: Harness, name: str, root: Path) -> SkillOutcome:
        target = root / name
        content_hash = self._source.content_hash(name)
        if SkillSource.read_manifest(target) == content_hash:
            return SkillOutcome(harness, name, "current", target)
        existed_before = target.exists()
        self._source.deposit(name, target)
        action: Action = "upgraded" if existed_before else "deposited"
        return SkillOutcome(harness, name, action, target)

    def _status_one(self, harness: Harness, name: str, root: Path) -> SkillOutcome:
        target = root / name
        if not target.is_dir():
            return SkillOutcome(harness, name, "absent", target)
        content_hash = self._source.content_hash(name)
        matches = SkillSource.read_manifest(target) == content_hash
        action: Action = "current" if matches else "stale"
        return SkillOutcome(harness, name, action, target)

    @staticmethod
    def _remove_one(harness: Harness, name: str, root: Path) -> SkillOutcome:
        """Remove *name* from *root* — but only a directory quarry itself deposited.

        A same-named directory that carries no ``.quarry-skill.json`` manifest
        is left alone and reported ``"foreign"``. Every marketplace-layout
        Claude Code plugin ships a ``plugin/skills/`` tree, so a name match
        alone proves nothing about provenance — a bare ``target.is_dir()``
        check here would ``rmtree`` a directory this installer never wrote,
        including one holding real, unrelated user data.
        """
        target = root / name
        if not target.is_dir():
            return SkillOutcome(harness, name, "absent", target)
        if SkillSource.read_manifest(target) is None:
            return SkillOutcome(harness, name, "foreign", target)
        SkillSource.remove_tree(target)
        return SkillOutcome(harness, name, "removed", target)
