"""A tree's ignore files: which of its paths git's rules ignore."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Self, final

from quarry.capture_directory import CaptureDirectory
from quarry.ignore_pattern import IgnoreFile

# Glob-style ignores applied below every user rule.  Scratch/VCS/build/cache
# *directory names* (``node_modules``, ``.venv``, ``dist``, ``*.egg-info``…) are
# NOT listed here: :class:`~quarry.scratch_paths.ScratchGuard` prunes them by
# name in the walk, so keeping them here too would be duplicated bookkeeping.
_DEFAULT_IGNORE_PATTERNS: Final[list[str]] = [
    "*.pyc",
    ".DS_Store",
    # Defense-in-depth for the captures dir.  The walk already prunes every
    # dot-prefixed directory (``.punt-labs``) before patterns are matched, so
    # this entry is redundant belt-and-braces — it records the intent that
    # scrubbed captures (the daemon's <repo>-captures) must never be folded into
    # the project's MAIN collection by directory sync.  Shared with
    # quarry.gitignore's own ``.gitignore``-writing entry so the two never drift.
    *CaptureDirectory.IGNORE_PATTERNS,
]


@final
class IgnoreTree:
    """Apply every directory's ignore files below one root, as git does.

    Each directory's files are read once, the first time a path below that
    directory is asked about, and cached for the instance's lifetime, so a
    live-watch hot path never re-reads them per event.  A change to an ignore
    file is picked up by the NEXT instance, not mid-session.
    """

    __slots__ = ("_directory", "_files")

    _directory: Path
    _files: dict[tuple[str, ...], IgnoreFile | None]

    def __new__(cls, directory: Path) -> Self:
        """Load the root's ignore files and start an empty per-directory cache."""
        self = super().__new__(cls)
        self._directory = directory
        self._files = {}
        self._file(())
        return self

    def ignores(self, rel: tuple[str, ...], *, is_dir: bool) -> bool:
        """Return whether the deepest ignore file that matches *rel* ignores it.

        Only the ignore files of *rel*'s own ancestors are consulted, each
        against the path relative to its directory; whether an ancestor
        directory is itself excluded is the caller's question.
        """
        for depth in range(len(rel) - 1, -1, -1):
            ignore_file = self._file(rel[:depth])
            if ignore_file is None:
                continue
            verdict = ignore_file.verdict("/".join(rel[depth:]), is_dir=is_dir)
            if verdict is not None:
                return verdict
        return False

    def _file(self, rel_dir: tuple[str, ...]) -> IgnoreFile | None:
        """Return *rel_dir*'s combined ignore files, or ``None`` when it has none."""
        if rel_dir not in self._files:
            defaults = () if rel_dir else _DEFAULT_IGNORE_PATTERNS
            self._files[rel_dir] = IgnoreFile.load(
                self._directory.joinpath(*rel_dir), defaults
            )
        return self._files[rel_dir]
