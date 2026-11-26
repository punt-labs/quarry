"""The one ignore decision shared by discovery, live watch checks and scheduling.

A bulk directory scan (:class:`~quarry.sync_discovery.FileDiscovery`), the
per-path live checks, and the Linux watch scheduler
(:mod:`~quarry.daemon.inotify_prune`) all ask :meth:`IgnoreRules.keeps` whether a
root-relative path survives.  The answer follows git: every directory's
``.gitignore`` and ``.quarryignore`` apply to paths relative to that
directory (:class:`~quarry.ignore_tree.IgnoreTree`), deeper files take
precedence, and nothing below an excluded directory can be re-included.  On
top of git's rules come the built-in scratch defaults, the
:class:`~quarry.scratch_paths.ScratchGuard` directory names, and the skipping
of every hidden (dot-prefixed) name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from quarry.ignore_tree import IgnoreTree
from quarry.scratch_paths import ScratchGuard


@final
class IgnoreRules:
    """Decide which paths under one registered root are kept (git semantics).

    The verdict for a directory is cached, so the observer-thread hot path
    does a dictionary lookup per ancestor, not a re-match.
    """

    __slots__ = ("_guard", "_kept_dirs", "_tree")

    _guard: ScratchGuard
    _tree: IgnoreTree
    _kept_dirs: dict[tuple[str, ...], bool]

    def __new__(cls, directory: Path, guard: ScratchGuard) -> Self:
        """Load the root's ignore files and start an empty directory cache."""
        self = super().__new__(cls)
        self._guard = guard
        self._tree = IgnoreTree(directory)
        self._kept_dirs = {}
        return self

    def keeps(self, rel: tuple[str, ...], *, is_dir: bool) -> bool:
        """Return whether the root-relative path *rel* survives every ignore rule.

        *rel* is the path's segments below the root (empty for the root
        itself, which is always kept).  A path is dropped when it or any
        ancestor directory is hidden, a scratch directory name, or ignored by
        the ignore files; an ancestor's exclusion is final.
        """
        if not rel:
            return True
        return self._dir_kept(rel[:-1]) and self._entry_kept(rel, is_dir=is_dir)

    def _dir_kept(self, rel_dir: tuple[str, ...]) -> bool:
        """Return whether the walk would enter *rel_dir* (cached)."""
        if not rel_dir:
            return True
        if rel_dir not in self._kept_dirs:
            self._kept_dirs[rel_dir] = self._dir_kept(rel_dir[:-1]) and (
                self._entry_kept(rel_dir, is_dir=True)
            )
        return self._kept_dirs[rel_dir]

    def _entry_kept(self, rel: tuple[str, ...], *, is_dir: bool) -> bool:
        """Apply the name rules and ignore files to *rel*, its parents being kept."""
        name = rel[-1]
        if name.startswith("."):
            return False
        if is_dir and self._guard.is_skip_name(name):
            return False
        return not self._tree.ignores(rel, is_dir=is_dir)
