"""Deposit quarry's canonical skills into every detected coding-agent harness.

The canonical content lives once, at ``<repo>/plugin/skills/<name>/`` (Claude
Code's marketplace plugin tree). This module copies that content, verbatim,
into the harness-specific locations that pi, opencode, and codex auto-load
skills from — Claude Code needs no deposit of its own, since it already reads
the canonical tree directly. Every deposit is version-stamped by content hash
so a re-run is a no-op unless the source changed, and safe: a deposit writes
into a temp sibling directory first and swaps it in with a backup, so an
interrupted write never leaves a half-written skill in place (bug class 1).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["Harness", "SkillOutcome", "SkillsInstaller"]

# Bumped only if the manifest's OWN shape changes (e.g. a new field); a skill
# content edit is tracked by its content hash, not this constant.
_MANIFEST_FORMAT_VERSION: Final = 1
_MANIFEST_NAME: Final = ".quarry-skill.json"

# The closed vocabulary a deposit/status/removal call reports. "unsupported"
# is Claude Code only (no deposit target); "current"/"stale" are status-only;
# "deposited"/"upgraded" are install-only; "removed"/"absent" are removal-only.
Action = Literal[
    "deposited", "upgraded", "current", "stale", "removed", "absent", "unsupported"
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


@final
class SkillsInstaller:
    """Deposit quarry's canonical skills into every targeted harness.

    Idempotent and version-stamped: each deposited skill directory carries a
    ``.quarry-skill.json`` manifest recording a content hash, so a re-run with
    unchanged source content is a no-op (``action="current"``) and a changed
    source overwrites the stale copy wholesale (``action="upgraded"``).
    """

    __slots__ = ("_home", "_source_dir")

    _source_dir: Path
    _home: Path

    def __new__(cls, source_dir: Path, home: Path) -> Self:
        self = super().__new__(cls)
        self._source_dir = source_dir
        self._home = home
        return self

    @staticmethod
    def locate_source(start: Path) -> Path:
        """Walk upward from *start* to find the repo's ``plugin/skills`` tree.

        Raises ``FileNotFoundError`` (PY-EH-8) rather than returning ``None``:
        a caller asking to install skills has nothing to copy without a
        source, and silently no-op-ing would hide a usage mistake (running
        outside a quarry checkout) as an empty success.
        """
        for candidate in (start, *start.parents):
            skills_dir = candidate / "plugin" / "skills"
            if skills_dir.is_dir():
                return skills_dir
        msg = (
            f"no plugin/skills/ tree found above {start} -- run "
            "'quarry skills install' from inside a quarry checkout."
        )
        raise FileNotFoundError(msg)

    def detected_harnesses(self) -> tuple[Harness, ...]:
        """Return every harness that looks installed on this machine."""
        return tuple(harness for harness in Harness if harness.detected(self._home))

    def install(self, harness: Harness) -> tuple[SkillOutcome, ...]:
        """Deposit every canonical skill into *harness*; idempotent."""
        root = harness.deposit_root(self._home)
        if root is None:
            return self._unsupported(harness)
        return tuple(
            self._install_one(harness, name, root) for name in self._skill_names()
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
                self._status_one(harness, name, root) for name in self._skill_names()
            )
        return tuple(outcomes)

    def remove(self, harness: Harness) -> tuple[SkillOutcome, ...]:
        """Delete every deposited skill for *harness*; idempotent."""
        root = harness.deposit_root(self._home)
        if root is None:
            return self._unsupported(harness)
        return tuple(
            self._remove_one(harness, name, root) for name in self._skill_names()
        )

    def remove_all(self) -> tuple[SkillOutcome, ...]:
        """Delete every deposited skill from every harness; idempotent."""
        outcomes: list[SkillOutcome] = []
        for harness in Harness:
            outcomes.extend(self.remove(harness))
        return tuple(outcomes)

    def _skill_names(self) -> tuple[str, ...]:
        """Return every canonical skill's name, sorted, that carries a SKILL.md."""
        return tuple(
            sorted(
                path.name
                for path in self._source_dir.iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            )
        )

    def _unsupported(self, harness: Harness) -> tuple[SkillOutcome, ...]:
        return tuple(
            SkillOutcome(harness, name, "unsupported", None)
            for name in self._skill_names()
        )

    def _install_one(self, harness: Harness, name: str, root: Path) -> SkillOutcome:
        target = root / name
        digest = self._hash_tree(self._source_dir / name)
        if self._read_manifest(target) == digest:
            return SkillOutcome(harness, name, "current", target)
        existed_before = target.exists()
        self._deposit(self._source_dir / name, target, digest)
        action: Action = "upgraded" if existed_before else "deposited"
        return SkillOutcome(harness, name, action, target)

    def _status_one(self, harness: Harness, name: str, root: Path) -> SkillOutcome:
        target = root / name
        if not target.is_dir():
            return SkillOutcome(harness, name, "absent", target)
        digest = self._hash_tree(self._source_dir / name)
        action: Action = "current" if self._read_manifest(target) == digest else "stale"
        return SkillOutcome(harness, name, action, target)

    @staticmethod
    def _remove_one(harness: Harness, name: str, root: Path) -> SkillOutcome:
        target = root / name
        if not target.is_dir():
            return SkillOutcome(harness, name, "absent", target)
        shutil.rmtree(target)
        return SkillOutcome(harness, name, "removed", target)

    @staticmethod
    def _hash_tree(skill_dir: Path) -> str:
        """Return a short, order-independent content hash of *skill_dir*."""
        digest = hashlib.sha256()
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(skill_dir).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    @staticmethod
    def _read_manifest(target: Path) -> str | None:
        """Return the deposited content hash, or ``None`` if absent/unreadable.

        ``None`` is the documented "no valid manifest here" contract (a fresh
        target, a hand-edited one, or a corrupt file all look the same to an
        idempotent re-install: deposit fresh), not an error the caller handles.
        """
        manifest = target / _MANIFEST_NAME
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        content_hash = data.get("content_hash") if isinstance(data, dict) else None
        return content_hash if isinstance(content_hash, str) else None

    @staticmethod
    def _deposit(source_dir: Path, target: Path, content_hash: str) -> None:
        """Copy *source_dir* to *target* and stamp its manifest, swapped in atomically.

        Writes into a temp sibling first, then swaps the old target (if any)
        to a backup sibling before renaming the temp into place — an
        interrupted copy never reaches *target*, and a failed rename restores
        the backup rather than leaving *target* missing (bug class 1).
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.tmp-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(source_dir, tmp)
        manifest = {
            "content_hash": content_hash,
            "format_version": _MANIFEST_FORMAT_VERSION,
        }
        (tmp / _MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

        backup = target.parent / f".{target.name}.bak-{os.getpid()}"
        if backup.exists():
            shutil.rmtree(backup)
        had_target = target.exists()
        if had_target:
            target.rename(backup)
        try:
            tmp.rename(target)
        except OSError:
            if had_target:
                backup.rename(target)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        else:
            if had_target:
                shutil.rmtree(backup)
