"""File discovery for directory sync: walk, filter, hash."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from quarry.ignore_spec import IgnoreRules
from quarry.registered_root import RegisteredRoot

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_HASH_CHUNK_SIZE: Final[int] = 1 << 20  # 1 MiB


@final
class FileDiscovery:
    """Discover indexable files under a directory, respecting ignore rules."""

    __slots__ = ("_root", "_rules", "_walk_complete")

    _root: RegisteredRoot
    _rules: IgnoreRules
    _walk_complete: bool

    def __new__(cls, directory: Path) -> Self:
        """Resolve the root and load its ignore rules."""
        self = super().__new__(cls)
        self._root = RegisteredRoot(directory)
        self._rules = IgnoreRules(directory, self._root.guard)
        self._walk_complete = True
        return self

    @property
    def root_available(self) -> bool:
        """Whether the registered root resolved and is not a refused scratch root.

        When ``False``, :meth:`discover` returns empty for a reason OTHER than
        "every file was deleted" (the root could not be resolved, or is a
        refused temp/scratch tree). Callers that compute deletions from an empty
        discovery MUST fail closed on this — otherwise a transient root blip
        reads as a full collection wipe.
        """
        return self._root.available

    @property
    def discovery_reliable(self) -> bool:
        """Whether the last :meth:`discover` saw a complete, resolvable tree.

        ``False`` when the root could not be resolved / is a refused scratch tree
        (:attr:`root_available`) OR the ``os.walk`` enumeration hit an error
        (permission loss, ``ESTALE``, or the directory vanishing mid-walk) — any
        of which yields an empty or partial discovery for a reason OTHER than
        deletion. Callers computing deletions MUST fail closed on this: an
        incomplete disk view is not evidence that files were removed. Valid only
        after :meth:`discover` has run.
        """
        return self.root_available and self._walk_complete

    def _note_walk_error(self, exc: OSError) -> None:
        """``os.walk`` onerror hook: a dir could not be listed → walk incomplete."""
        logger.warning("Directory walk error under %s: %s", self._root.directory, exc)
        self._walk_complete = False

    def discover(self, extensions: frozenset[str]) -> list[Path]:
        """Recursively find files matching *extensions* under the directory.

        Applies :meth:`~quarry.ignore_spec.IgnoreRules.keeps` to every entry:
        ``.gitignore`` and ``.quarryignore`` at every level with git's
        semantics, the :class:`~quarry.scratch_paths.ScratchGuard` always-skip
        names (``venv``, ``node_modules``, ``htmlcov``…), the built-in glob
        defaults, and no hidden names (dotfiles, macOS resource forks
        ``._*``, files inside ``.Trash``, ``.git``, etc.).

        A symlinked directory is never entered. A symlinked file is kept only
        when its resolved target is inside the root and itself passes the
        same rules; otherwise it is dropped and logged as a warning.

        Returns absolute paths, sorted for deterministic order.
        """
        self._walk_complete = True
        result: list[Path] = []
        for dirpath, filenames in self._pruned_walk():
            result.extend(self._keep_files(dirpath, filenames, extensions))
        return result

    def is_watchable_dir(self, dirpath: Path) -> bool:
        """Return whether *dirpath* (a directory under the root) survives pruning.

        Ancestor-aware: a directory reached via watchdog's un-prunable
        backfill walk (``_recursive_simulate``) can be several levels below a
        pruned ancestor whose rejection does not stop that walk from
        descending. :meth:`~quarry.ignore_spec.IgnoreRules.keeps` checks every
        segment from the root down, exactly as :meth:`discover`'s walk does,
        and a symlinked segment is never watched, as that walk never enters it.
        """
        if not self._root.available:
            return False
        try:
            rel = self._root.relative(dirpath)
        except ValueError:
            return False
        return not self._root.crosses_symlink(rel.parts) and self._rules.keeps(
            rel.parts, is_dir=True
        )

    def _pruned_walk(self) -> Iterator[tuple[Path, list[str]]]:
        """Walk the root, pruning ignored directories in place: the ONE walk.

        :meth:`discover` is the sole consumer, so there is exactly one place
        that decides which directories a bulk-scan walk enters. Yields
        ``(dirpath, filenames)`` for every directory that survives pruning;
        ``dirnames`` is mutated in place (the documented ``os.walk``
        contract) so a pruned subtree is never entered.
        """
        if not self._root.available:
            return
        for dirpath_str, dirnames, filenames in os.walk(
            self._root.directory, onerror=self._note_walk_error
        ):
            dirpath = Path(dirpath_str)
            rel_dir = self._root.relative(dirpath).parts
            dirnames[:] = sorted(
                name
                for name in dirnames
                if self._rules.keeps((*rel_dir, name), is_dir=True)
            )
            yield dirpath, filenames

    def _keep_files(
        self, dirpath: Path, filenames: list[str], extensions: frozenset[str]
    ) -> Iterator[Path]:
        """Yield the absolute path of each indexable file directly in *dirpath*."""
        rel_dir = self._root.relative(dirpath).parts
        for filename in sorted(filenames):
            filepath = dirpath / filename
            if filepath.suffix.lower() not in extensions:
                continue
            if not self._rules.keeps((*rel_dir, filename), is_dir=False):
                continue
            if filepath.is_symlink() and not self._link_kept(filepath):
                continue
            yield filepath.absolute()

    def is_indexable(self, path: Path, extensions: frozenset[str]) -> bool:
        """Return whether *path* would be indexed by :meth:`discover` (live == bulk).

        Applies discover's rules to ONE path so a live watch edit and a bulk
        scan agree: the suffix must be supported, the path must survive
        :meth:`~quarry.ignore_spec.IgnoreRules.keeps` (which checks every
        ancestor), no ancestor directory may be a symlink, and the file's
        resolved target must lie inside the real root and survive the same
        rules (a symlink whose target escapes the tree is rejected, closing
        the path-escape leak).
        """
        if not self._root.available:
            return False
        if path.suffix.lower() not in extensions:
            return False
        try:
            rel = self._root.relative(path)
            target = self._root.target_parts(path)
        except ValueError:
            return False
        if self._root.crosses_symlink(rel.parent.parts):
            return False
        return self._rules.keeps(rel.parts, is_dir=False) and self._rules.keeps(
            target, is_dir=False
        )

    def _link_kept(self, link: Path) -> bool:
        """Return whether the symlinked file *link*'s target is inside and kept."""
        try:
            target = self._root.target_parts(link)
        except ValueError as exc:
            logger.warning("Skipping symlink: %s", exc)
            return False
        return self._rules.keeps(target, is_dir=False)

    def is_deletable(self, path: Path, extensions: frozenset[str]) -> bool:
        """Return whether a REMOVED *path* names a document worth a delete job.

        A delete reads no content (no symlink-escape risk) and
        ``delete_document`` is idempotent, so this is a purely lexical check —
        the file is already gone and cannot be resolved.  A false accept is a
        harmless no-op; the disk-vs-registry reconcile is the robust backstop.
        """
        if not self._root.available or path.suffix.lower() not in extensions:
            return False
        try:
            rel = self._root.relative(path)
        except ValueError:
            return False
        return not self._root.hidden_or_skipped(rel)

    @staticmethod
    def content_hash(path: Path) -> str:
        """Return a fast content hash of *path* for change detection.

        Uses ``blake2b`` with a 16-byte digest (128 bits).
        """
        h = hashlib.blake2b(digest_size=16)
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()
