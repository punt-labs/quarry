"""The ``.punt-labs/quarry/enabled`` marker that signals repo enablement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["EnabledMarker"]


@final
class EnabledMarker:
    """The presence marker for quarry being enabled in a repo.

    quarry is enabled in a repo when ``<repo>/.punt-labs/quarry/enabled``
    exists (tool-enable-disable.md § 2.7). ``enable`` writes it; ``disable``
    deletes it. This is a distinct signal from directory presence — the
    vendored guide persists after ``disable`` (the dormant state, § 2.9), so
    directory presence cannot mean "enabled." Both hook gates and ``punt
    audit`` read this one file. Per § 2.11 the marker is a commit point:
    :class:`quarry.enablement.Enablement` writes it only after the repo
    ``@.punt-labs/quarry/CLAUDE.md`` import is effective, so marker present ⇒
    import present. The reverse can differ mid-operation (import written, marker
    not) — the recoverable state a re-run reconciles.

    Access is symlink-safe against an *untrusted* repo: the marker is a
    :class:`quarry.safe_paths.SafeRepoPath`, so neither a symlinked leaf nor a
    symlinked ancestor can redirect the create/stat/unlink outside the repo.
    """

    __slots__ = ("_file",)

    _file: SafeRepoPath

    _RELATIVE = (".punt-labs", "quarry", "enabled")

    def __new__(cls, root: Path) -> Self:
        """Anchor the presence marker under the repo's guarded tool-state tree."""
        self = super().__new__(cls)
        self._file = SafeRepoPath(root, cls._RELATIVE)
        return self

    @property
    def path(self) -> Path:
        """Return the marker file path."""
        return self._file.path

    def is_present(self) -> bool:
        """Return whether a regular-file marker exists (quarry is enabled here)."""
        return self._file.is_regular_file()

    def write(self) -> bool:
        """Create the marker at mode ``0644``; return whether it was created.

        A present regular marker is left untouched — no mtime bump — and
        ``False`` is returned, so re-enabling is a true idempotent no-op the
        caller can report as such. A non-regular entry at the marker path
        (symlink, directory, …) is refused with ``ValueError`` rather than
        silently reported as present, which would strand an import with no real
        marker (the § 2.11 forbidden state).
        """
        return self._file.create_exclusive("", mode=0o644)

    def remove(self) -> bool:
        """Delete the marker; return whether a regular-file marker was removed.

        A symlink or other non-regular entry at the marker path is not the signal
        quarry wrote, so it is left in place (``False``). Leaves the rest of
        ``.punt-labs/quarry/`` intact — ``disable`` removes only the signal it
        wrote, never the dormant vendored guide (§ 2.9).
        """
        return self._file.remove()
