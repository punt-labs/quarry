"""Quarry's allow rules in a project's ``.claude/settings.json`` (#552).

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

_COMMANDS: Final = (
    "explain",
    "find",
    "help",
    "ingest",
    "learn",
    "quarry",
    "remember",
    "source",
)

# The deterministic rule set: enable adds every missing member, disable removes
# these exact values (§ 2.8). Changing it strands rules on disable, so a rule
# retired here must stay in the set until no enabled repo can still carry it.
QUARRY_RULES: Final = (
    "mcp__quarry__*",
    "Bash(quarry:*)",
    *(f"Skill({name})" for name in _COMMANDS),
)

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
        """Edit the allow list in place and write it back if it changed."""
        settings = self._read()
        allow = self._allow_list(settings)
        if add:
            changed = tuple(rule for rule in QUARRY_RULES if rule not in allow)
            allow.extend(changed)
        else:
            changed = tuple(rule for rule in QUARRY_RULES if rule in allow)
            allow[:] = [rule for rule in allow if rule not in QUARRY_RULES]
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

    @staticmethod
    def _allow_list(settings: dict[str, object]) -> list[object]:
        """Return ``permissions.allow``, creating it if absent; refuse bad shapes.

        A ``permissions`` or ``allow`` of the wrong type is the user's data,
        so it is reported rather than overwritten. Containers created here are
        only written when a rule changes.
        """
        permissions = settings.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            msg = ".claude/settings.json 'permissions' is not an object"
            raise ValueError(msg)
        allow = permissions.setdefault("allow", [])
        if not isinstance(allow, list):
            msg = ".claude/settings.json 'permissions.allow' is not a list"
            raise ValueError(msg)
        return allow
