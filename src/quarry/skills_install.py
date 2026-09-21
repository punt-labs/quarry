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

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, final

from quarry.own_checkout import OwnCheckout
from quarry.skills_source import SkillSource

__all__ = ["Harness", "SkillOutcome", "SkillsInstaller"]

# The closed vocabulary a deposit/status/removal call reports. "unsupported"
# is Claude Code only (no deposit target); "current"/"stale" are status
# outcomes reported alongside it; "deposited"/"upgraded" are install-only;
# "removed"/"absent" are removal-only; "foreign" is shared by all three
# write/read paths (install, status, remove) — a same-named directory exists
# but carries no quarry manifest, so it was refused/reported-as-is rather
# than overwritten or deleted (see PY-EH-8/safety note on
# :meth:`SkillsInstaller._is_foreign`).
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
        return self._codex_home(home) / "skills"

    def detected(self, home: Path) -> bool:
        """Return whether this harness looks installed on *home*'s machine."""
        if self is Harness.CLAUDE:
            return (home / ".claude").is_dir()
        if self is Harness.PI:
            return (home / ".pi").is_dir()
        if self is Harness.OPENCODE:
            return (home / ".config" / "opencode").is_dir()
        return self._codex_home(home).is_dir()

    @staticmethod
    def _codex_home(home: Path) -> Path:
        """Codex's own root: ``$CODEX_HOME`` if set, else ``<home>/.codex``.

        Codex itself resolves its home this way; hardcoding ``~/.codex``
        would deposit nowhere codex actually loads from on a machine where
        the operator points ``CODEX_HOME`` elsewhere.
        """
        codex_home = os.environ.get("CODEX_HOME")
        return Path(codex_home) if codex_home else home / ".codex"


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
        """Walk upward from *start* to find quarry's OWN ``plugin/skills`` tree.

        Delegates to :meth:`SkillSource.locate` for the shape match, then
        refuses the result unless the enclosing repo is provably quarry's
        OWN checkout (:class:`quarry.own_checkout.OwnCheckout`). Shape
        alone proves nothing -- every marketplace-layout Claude Code plugin
        (lux, prfaq, dungeon, punt-kit, ...) ships an identically-shaped
        ``plugin/skills/`` tree, so running ``quarry skills install`` from a
        hostile or unrelated checkout would otherwise deposit THAT repo's
        skills as quarry's own (mirrors the disable-side ownership guard in
        :mod:`quarry.enablement`). Raising ``FileNotFoundError`` here, the
        same type :meth:`SkillSource.locate` already raises for "no tree
        found," keeps one error type for every "nothing installable here"
        outcome at this boundary.
        """
        skills_dir = SkillSource.locate(start)
        repo_root = skills_dir.parent.parent
        if not OwnCheckout(repo_root).confirmed():
            msg = (
                f"{skills_dir} is not part of quarry's own checkout -- "
                "refusing to install skills from an untrusted source."
            )
            raise FileNotFoundError(msg)
        return skills_dir

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
        there (see the safety note on :meth:`_is_foreign`).
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
        """Deposit *name* into *root* -- but never overwrite a foreign directory.

        A same-named directory with no readable quarry manifest is left
        alone and reported ``"foreign"`` rather than upgraded: :meth:`deposit`
        renames the live directory aside and deletes it on success, the same
        destructive shape as :meth:`_remove_one`'s ``rmtree``, so it needs the
        identical ownership guard (see :meth:`_is_foreign`) -- otherwise a
        harness-root collision with an unrelated directory of the same name
        would clobber it silently on the very first install.
        """
        target = root / name
        content_hash = self._source.content_hash(name)
        manifest_hash = SkillSource.read_manifest(target)
        if manifest_hash == content_hash:
            return SkillOutcome(harness, name, "current", target)
        if self._is_foreign(target):
            return SkillOutcome(harness, name, "foreign", target)
        existed_before = target.exists()
        self._source.deposit(name, target)
        action: Action = "upgraded" if existed_before else "deposited"
        return SkillOutcome(harness, name, action, target)

    def _status_one(self, harness: Harness, name: str, root: Path) -> SkillOutcome:
        target = root / name
        if not target.is_dir():
            return SkillOutcome(harness, name, "absent", target)
        if self._is_foreign(target):
            return SkillOutcome(harness, name, "foreign", target)
        content_hash = self._source.content_hash(name)
        matches = SkillSource.read_manifest(target) == content_hash
        action: Action = "current" if matches else "stale"
        return SkillOutcome(harness, name, action, target)

    @staticmethod
    def _remove_one(harness: Harness, name: str, root: Path) -> SkillOutcome:
        """Remove *name* from *root* — but only a directory quarry itself deposited.

        See :meth:`_is_foreign` for why a name match alone never proves
        provenance.
        """
        target = root / name
        if not target.is_dir():
            return SkillOutcome(harness, name, "absent", target)
        if SkillsInstaller._is_foreign(target):
            return SkillOutcome(harness, name, "foreign", target)
        SkillSource.remove_tree(target)
        return SkillOutcome(harness, name, "removed", target)

    @staticmethod
    def _is_foreign(target: Path) -> bool:
        """Whether *target* exists but carries no readable quarry manifest.

        The one "not provably ours" signal shared by install (refuse to
        overwrite), status (report honestly), and remove (refuse to
        delete). Every marketplace-layout Claude Code plugin ships an
        identically-shaped ``plugin/skills/`` tree, so a bare directory-name
        match proves nothing about provenance — a missing, hand-edited, or
        corrupt manifest all collapse to the same "no proof quarry deposited
        this" answer (PY-EH-8: :meth:`SkillSource.read_manifest` already
        documents ``None`` as that one contract for all three causes).
        """
        return target.is_dir() and SkillSource.read_manifest(target) is None
