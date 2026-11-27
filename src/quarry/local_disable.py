"""The repo-local steps of ``quarry disable``: the config and the guarded teardown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.config_verdict import ConfigVerdict
from quarry.enabled_marker import EnabledMarker
from quarry.enablement import DisablementResult, Enablement
from quarry.guidance import REPO_IMPORT_LINE
from quarry.marker_search import MarkerSearch

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ConfigDisposal", "GuardedDisable"]


@final
@dataclass(frozen=True, slots=True)
class ConfigDisposal:
    """Whether a disable removed the project config or left it in place.

    A directory under a parent registration leaves its config when it changes
    what the hooks do (the parent's capture still reads it, and hook defaults
    apply once it is gone, so removing a config that opts out of a capture would
    turn that capture back on), when the hooks cannot read it (``"unreadable"``),
    or when it is behind a symlink (``"linked"``); ``why`` then holds the
    :class:`~quarry.config_verdict.ConfigVerdict` value.  An uncovered directory always
    has its config removed.
    """

    removed: bool
    kept: bool
    why: str = ""

    @classmethod
    def settle(cls, directory: Path, *, covered: bool) -> Self:
        """Remove or keep ``directory``'s config and report which happened.

        A symlinked ``.punt-labs`` ancestor makes the unlink refuse; that is not
        a real in-repo config, so it counts as absent and the disable goes on to
        prune the ``@``-import a prior teardown already acted on.
        """
        verdict = ConfigVerdict.of(directory) if covered else ConfigVerdict.ABSENT
        if verdict.left_in_place:
            return cls(removed=False, kept=True, why=verdict.value)
        try:
            return cls(removed=ConfigVerdict.discard(directory), kept=False)
        except ValueError:
            return cls(removed=False, kept=False)


@final
class GuardedDisable:
    """Run :meth:`Enablement.disable` only on a repo that has something to remove.

    The disable takes the ``CLAUDE.md`` edit lock, which leaves a lock file that
    is never removed; on a repo that never had quarry enabled that file is the
    only trace of the disable. Asking first, without a lock, keeps it out.
    """

    __slots__ = ("_enablement", "_root")

    _root: Path
    _enablement: Enablement

    def __new__(cls, root: Path) -> Self:
        """Bind the guard and the teardown it protects to one repo root."""
        self = super().__new__(cls)
        self._root = root
        self._enablement = Enablement(root)
        return self

    def run(self) -> DisablementResult:
        """Tear down the marker and import, or report that none was there."""
        if self.present():
            return self._enablement.disable()
        return DisablementResult(import_pruned=False, enabled_marker_removed=False)

    def capturing_marker(self, registered: str) -> str:
        """Return the directory whose marker still captures the root, or empty.

        The search is the session-start hook's own, so the answer is what the
        next session in the root will find; ask it after :meth:`run`.
        """
        found = MarkerSearch(self._root, registered).found_at() if registered else None
        return str(found or "")

    def present(self) -> bool:
        """Return whether a marker or the import line may be there to remove.

        A plain substring match errs toward ``True`` (a fenced copy of the line
        also matches), which only costs the lock the teardown would have taken
        anyway; unreadable text is treated the same way.
        """
        return EnabledMarker(self._root).is_present() or self._mentions_import()

    def _mentions_import(self) -> bool:
        claude = self._root / "CLAUDE.md"
        if not claude.is_file():
            return False
        try:
            return REPO_IMPORT_LINE in claude.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return True
