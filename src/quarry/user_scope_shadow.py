"""Prune a stray user-scope ``mcpServers.quarry`` entry from ``~/.claude.json``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.atomic_file import AtomicFile
from quarry.file_lock import FileLock
from quarry.home_paths import QuarryHome
from quarry.json_file import JsonFile
from quarry.mcp_constants import MCP_SERVER_NAME as _MCP_SERVER_NAME
from quarry.shadow_prune import ShadowPrune, ShadowPruneKind

if TYPE_CHECKING:
    from quarry.doctor_mcp import ClaudeCodeMcp


@final
class UserScopeShadow:
    """Prune a stray ``mcpServers.quarry`` entry from ``~/.claude.json``.

    ``quarry install`` once wrote a user-scope entry unconditionally
    (PL-999); the quarry Claude Code plugin now owns the server
    (tool-enable-disable.md §2.6/§2.8), so that entry is pure shadow once the
    plugin is confirmed registered *at user scope* -- a project-scope-only
    plugin install does not own the server for this user, so the fallback
    stays. A collaborator of :class:`~quarry.doctor_mcp.ClaudeCodeMcp` rather
    than a method on it: it owns a disjoint piece of state (the
    ``~/.claude.json`` path) and a disjoint concern (pruning a stray write,
    not registering or checking the plugin-owned server).

    ``~/.claude.json`` is Claude Code's own primary state file and grows
    unbounded with session history, so it is read with a generous cap rather
    than :attr:`JsonFile.MAX_BYTES` -- that cap guards untrusted config, and
    this file is trusted by convention, not hostile.
    """

    __slots__ = ("_claude_json_path", "_plugin")

    # Claude Code's own history-bearing state file has been reported well past
    # the 1 MiB untrusted-config cap (60+ MiB); this bound only rules out a
    # truly pathological file, not a real one.
    _CLAUDE_JSON_MAX_BYTES: ClassVar[int] = 128 * 1024 * 1024

    _plugin: ClaudeCodeMcp
    _claude_json_path: Path

    def __new__(
        cls, plugin: ClaudeCodeMcp, claude_json_path: Path | None = None
    ) -> Self:
        self = super().__new__(cls)
        self._plugin = plugin
        self._claude_json_path = claude_json_path or (
            QuarryHome.home() / ".claude.json"
        )
        return self

    def prune(self) -> ShadowPrune:
        """Remove the stray entry; a no-op, never an error, when not applicable.

        Opportunistic: a no-op when the plugin is not registered at user
        scope (the ``--no-plugin`` path, and a project-scope-only install,
        still need their own user-scope entry), the file is absent, or the
        ``quarry`` key is already gone -- the file is then never touched.

        ``~/.claude.json`` is Claude Code's own primary state file; this
        process's :class:`~quarry.file_lock.FileLock` only serializes against
        other punt tools, not against Claude Code's own writer, which does not
        take it. A concurrent Claude-Code write landing between the read below
        and the rename can still be lost -- accepted because this prune runs
        only from install/doctor/uninstall (rare) and the read-to-write window
        is the smallest this method can make it.
        """
        if not self._plugin.check().passed:
            return ShadowPrune(ShadowPruneKind.ABSENT)
        # Every I/O path -- taking the lock, reading, and the atomic write --
        # is caught here so prune() honors its "never an error" contract: a
        # read-only or full home must not propagate out and abort quarry
        # uninstall before the service is removed. The stray entry is cosmetic;
        # an I/O failure is reported as UNREADABLE, never raised. ValueError
        # covers a malformed ~/.claude.json from JsonFile.load.
        try:
            with FileLock.shared(self._claude_json_path):
                return self._strip_entry()
        except (OSError, ValueError) as exc:
            return ShadowPrune(
                ShadowPruneKind.UNREADABLE, JsonFile.why(exc, self._claude_json_path)
            )

    def _strip_entry(self) -> ShadowPrune:
        """Delete the ``quarry`` key under a held lock; the file-touch boundary.

        Raises ``OSError``/``ValueError`` on an unreadable or unwritable file;
        :meth:`prune` catches both and reports them as UNREADABLE.
        """
        if not self._claude_json_path.exists():
            return ShadowPrune(ShadowPruneKind.ABSENT)
        config = JsonFile.load(
            self._claude_json_path, max_bytes=self._CLAUDE_JSON_MAX_BYTES
        )
        servers = config.get("mcpServers")
        if not isinstance(servers, dict) or _MCP_SERVER_NAME not in servers:
            return ShadowPrune(ShadowPruneKind.ABSENT)
        if not self._is_quarrys_own_entry(servers[_MCP_SERVER_NAME]):
            # A user deliberately set a different quarry command/remote. Deleting
            # it would be silent data loss; leave it and let the user decide.
            return ShadowPrune(ShadowPruneKind.PRESERVED)
        del servers[_MCP_SERVER_NAME]
        AtomicFile(self._claude_json_path).replace(json.dumps(config, indent=2) + "\n")
        return ShadowPrune(ShadowPruneKind.REMOVED)

    @staticmethod
    def _is_quarrys_own_entry(entry: object) -> bool:
        """Return whether *entry* is the ``quarry mcp`` registration quarry writes.

        Matches a stdio entry whose command is the ``quarry`` binary (by
        basename, so an absolute path still matches) and whose args are exactly
        ``["mcp"]`` -- the shape :meth:`ClaudeCodeMcp._command` produces. Any
        other shape is a user's own entry and is never deleted.
        """
        if not isinstance(entry, dict):
            return False
        command = entry.get("command")
        if not isinstance(command, str) or Path(command).name != "quarry":
            return False
        return entry.get("args") == ["mcp"]
