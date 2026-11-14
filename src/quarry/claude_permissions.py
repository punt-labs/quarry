"""Quarry's allow and ask rules in a project's ``.claude/settings.json`` (#552).

``quarry enable`` is the operator's explicit opt-in, so it -- not plugin
startup -- grants Claude Code permission to call quarry's MCP tools and slash
commands without a prompt. The rules land in the enabled project's
``<repo>/.claude/settings.json`` only (tool-enable-disable.md § 2.8), never the
user-global ``~/.claude/settings.json``, and ``quarry disable`` removes exactly
the same set.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self, final

from quarry.file_lock import FileLock
from quarry.safe_paths import SafeRepoPath

__all__ = ["QUARRY_RULES", "PermissionChange", "ProjectPermissions"]

# Broad allow per surface with exact ask carve-outs (punt-kit permissions.md
# § 6): ask beats allow, so these verbs keep prompting while new safe commands
# work without a rule update. ``login`` retargets the user-global client config
# and sends QUARRY_API_KEY to the chosen host; ``logout`` and ``remote`` change
# that connection; ``install``/``uninstall`` manage the daemon; ``enable`` and
# ``disable`` change permissions in any repo; ``captures``/``skills`` write
# outside the repo; ``mcp`` starts a server; ``delete`` (CLI and MCP) drops
# remembered content that cannot be re-ingested. ``Bash(quarry -*)`` asks for a
# global option before the verb, which the verb rules would not match. The
# deterministic rule set:
# enable adds every missing member, disable removes these exact values (§ 2.8).
# A rule dropped from the set is no longer removed by disable; there is no
# cleanup code for it (punt-kit#396).
QUARRY_RULES: Final[dict[str, tuple[str, ...]]] = {
    "allow": ("Bash(quarry:*)", "mcp__quarry__*", "Skill(quarry:*)"),
    "ask": (
        "Bash(quarry -*)",
        *(
            f"Bash(quarry {verb}:*)"
            for verb in (
                "login",
                "logout",
                "remote",
                "install",
                "uninstall",
                "enable",
                "disable",
                "captures",
                "skills",
                "mcp",
                "delete",
                "use",
                "prune-mcp-shadow",
            )
        ),
        "mcp__quarry__delete",
    ),
}

_SETTINGS: Final = (".claude", "settings.json")


@dataclass(frozen=True, slots=True)
class PermissionChange:
    """What a grant or revoke did to the project's ``.claude/settings.json``.

    ``changed`` lists the rules added (grant) or removed (revoke); an empty
    tuple with no ``error`` means the file already matched. ``error`` explains
    why the file was left untouched.
    """

    changed: tuple[str, ...] = ()
    error: str = ""

    def lines(self, verb: str, *, unchanged: str = "") -> list[str]:
        """Return the summary line: *verb* the changed rules, else *unchanged*."""
        if self.error:
            return [f"  Permissions not changed: {self.error}"]
        if self.changed:
            rules = ", ".join(self.changed)
            return [f"  {verb} .claude/settings.json (this project only): {rules}"]
        return [unchanged] if unchanged else []


@final
class _RuleList:
    """One ``permissions.<tier>`` list of parsed settings, edited in place.

    A ``permissions`` or tier list of the wrong type is the user's data, so it
    is reported rather than overwritten. A missing tier is created only when
    rules are added, and containers are only written when a rule changes.
    """

    __slots__ = ("_owned", "_rules")

    _rules: list[object]
    _owned: tuple[str, ...]

    def __new__(cls, settings: dict[str, object], tier: str, *, create: bool) -> Self:
        permissions = settings.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            msg = ".claude/settings.json 'permissions' is not an object"
            raise ValueError(msg)
        rules = (
            permissions.setdefault(tier, []) if create else permissions.get(tier, [])
        )
        if not isinstance(rules, list):
            msg = f".claude/settings.json 'permissions.{tier}' is not a list"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._rules = rules
        self._owned = QUARRY_RULES[tier]
        return self

    def add_missing(self) -> tuple[str, ...]:
        """Append each quarry rule of this tier not yet present; return those added."""
        added = tuple(rule for rule in self._owned if rule not in self._rules)
        self._rules.extend(added)
        return added

    def remove_present(self) -> tuple[str, ...]:
        """Remove every quarry rule of this tier; return those removed."""
        removed = tuple(rule for rule in self._owned if rule in self._rules)
        self._rules[:] = [rule for rule in self._rules if rule not in self._owned]
        return removed


@final
class ProjectPermissions:
    """Add or remove :data:`QUARRY_RULES` in one repo's ``.claude/settings.json``.

    The read-modify-write holds a :class:`FileLock` (the file is shared with
    other tools), reads and writes through :class:`SafeRepoPath` so a planted
    symlink cannot redirect it out of the repo, appends in rule order without
    reordering existing entries, and does not write when nothing changes.
    Every other key and rule in the file is preserved.
    """

    __slots__ = ("_file", "_root")

    _file: SafeRepoPath
    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        self._file = SafeRepoPath(root, _SETTINGS)
        return self

    def grant(self) -> PermissionChange:
        """Append each missing quarry rule; return the rules added."""
        return self._update(add=True)

    def revoke(self) -> PermissionChange:
        """Remove every quarry rule; return the rules removed."""
        return self._update(add=False)

    def _update(self, *, add: bool) -> PermissionChange:
        """Apply the grant or revoke under the lock, reporting any refusal."""
        if self._root.resolve() == Path.home().resolve():
            return PermissionChange(
                error="the project is the home directory, whose .claude/"
                "settings.json is user-global"
            )
        try:
            if not self._settings_present() and not add:
                return PermissionChange()
            with FileLock.shared(self._file.path):
                return PermissionChange(changed=self._apply(add=add))
        except ValueError as exc:
            return PermissionChange(error=str(exc))
        except OSError as exc:
            return PermissionChange(error=exc.strerror or type(exc).__name__)

    def _settings_present(self) -> bool:
        """Return whether ``.claude/settings.json`` exists; refuse a linked dir.

        Runs before the lock is taken: :class:`FileLock` creates its lock file
        in ``.claude/``, so a ``.claude`` symlink would place it outside the
        repo, and a revoke with no settings file must not create ``.claude/``.
        """
        claude = self._file.path.parent
        try:
            if stat.S_ISLNK(os.lstat(claude).st_mode):
                msg = ".claude is a symlink; refusing to edit through it"
                raise ValueError(msg)
            os.lstat(self._file.path)
        except FileNotFoundError:
            return False
        return True

    def _apply(self, *, add: bool) -> tuple[str, ...]:
        """Edit each tier in place and write the file back if a rule changed."""
        settings = self._read()
        tiers = [_RuleList(settings, tier, create=add) for tier in QUARRY_RULES]
        changed = tuple(
            rule
            for tier in tiers
            for rule in (tier.add_missing() if add else tier.remove_present())
        )
        if changed:
            self._file.write_atomic(json.dumps(settings, indent=2) + "\n")
        return changed

    def _read(self) -> dict[str, object]:
        """Return the parsed settings, ``{}`` when absent; refuse a non-object."""
        try:
            data: object = json.loads(self._file.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            msg = f".claude/settings.json is not valid JSON ({exc.msg})"
            raise ValueError(msg) from exc
        if not isinstance(data, dict):
            msg = ".claude/settings.json is not a JSON object"
            raise ValueError(msg)
        return data
