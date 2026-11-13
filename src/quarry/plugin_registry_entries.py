"""One plugin key's scope-scanning registry entries."""

from __future__ import annotations

from typing import Self, final


@final
class PluginRegistryEntries:
    """The entries Claude Code lists for one plugin key, scope-aware.

    Claude Code lists one entry per scope a plugin is installed at: at most
    one ``scope: user`` (global) entry, plus one ``scope: project`` entry
    per directory it was installed into locally. Only the user-scope entry
    means the plugin owns the server for every session on this machine --
    a project-scope-only plugin must not be mistaken for a global install.
    """

    __slots__ = ("_entries",)

    _entries: tuple[object, ...]

    def __new__(cls, raw: list[object]) -> Self:
        self = super().__new__(cls)
        self._entries = tuple(raw)
        return self

    def is_well_formed(self) -> bool:
        """Return whether at least one entry is a dict Claude Code could read."""
        return any(isinstance(entry, dict) for entry in self._entries)

    def user_scope(self) -> dict[str, object] | None:
        """Return the ``scope: user`` entry, or ``None`` if there is none."""
        for entry in self._entries:
            if isinstance(entry, dict) and entry.get("scope") == "user":
                return entry
        return None
