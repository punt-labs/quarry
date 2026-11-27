"""The repo-local steps of ``quarry disable``: the config and the guarded teardown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.enabled_marker import EnabledMarker
from quarry.enablement import DisablementResult, Enablement
from quarry.guidance import REPO_IMPORT_LINE
from quarry.project_config import ProjectConfig
from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ConfigDisposal", "GuardedDisable"]

_RELATIVE = (".punt-labs", "quarry", "config.md")


@final
@dataclass(frozen=True, slots=True)
class ConfigDisposal:
    """Whether a disable removed the project config or kept it.

    A directory under a parent registration keeps its config when the file
    differs from the template: the parent's capture still reads it, and hook
    defaults apply once it is gone, so removing a config that opts out of a
    capture would turn that capture back on. An uncovered directory always has
    its config removed.
    """

    removed: bool
    kept: bool

    @classmethod
    def settle(cls, directory: Path, *, covered: bool) -> Self:
        """Remove or keep ``directory``'s config and report which happened.

        A symlinked ``.punt-labs`` ancestor makes the unlink refuse; that is not
        a real in-repo config, so it counts as absent and the disable goes on to
        prune the ``@``-import a prior teardown already acted on.
        """
        config = SafeRepoPath(directory, _RELATIVE)
        if covered and cls._customised(config):
            return cls(removed=False, kept=True)
        try:
            return cls(removed=config.remove(), kept=False)
        except ValueError:
            return cls(removed=False, kept=False)

    @staticmethod
    def _customised(config: SafeRepoPath) -> bool:
        """Return whether the config exists and is not provably the template.

        Unreadable text counts as customised: a file that cannot be shown to hold
        only defaults is not the tool's to delete.
        """
        if not config.is_regular_file():
            return False
        try:
            return config.read_text() != ProjectConfig().text
        except (OSError, ValueError, UnicodeDecodeError):
            return True


@final
class GuardedDisable:
    """Run :meth:`Enablement.disable` only on a repo that has something to remove.

    The disable takes the ``CLAUDE.md`` edit lock, which leaves a lock file that
    is never removed; on a repo that never had quarry enabled that file is the
    only trace of the disable. Asking first, without a lock, keeps it out.
    """

    __slots__ = ("_claude", "_enablement", "_marker")

    _enablement: Enablement
    _marker: EnabledMarker
    _claude: Path

    def __new__(cls, root: Path) -> Self:
        """Bind the guard and the teardown it protects to one repo root."""
        self = super().__new__(cls)
        self._enablement = Enablement(root)
        self._marker = EnabledMarker(root)
        self._claude = root / "CLAUDE.md"
        return self

    def run(self) -> DisablementResult:
        """Tear down the marker and import, or report that none was there."""
        if self.present():
            return self._enablement.disable()
        return DisablementResult(import_pruned=False, enabled_marker_removed=False)

    def present(self) -> bool:
        """Return whether a marker or the import line may be there to remove.

        A plain substring match errs toward ``True`` (a fenced copy of the line
        also matches), which only costs the lock the teardown would have taken
        anyway; unreadable text is treated the same way.
        """
        return self._marker.is_present() or self._mentions_import()

    def _mentions_import(self) -> bool:
        if not self._claude.is_file():
            return False
        try:
            return REPO_IMPORT_LINE in self._claude.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return True
