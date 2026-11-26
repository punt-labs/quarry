"""File discovery for directory sync: walk, filter, hash."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from quarry.ignore_spec import IgnoreRules
from quarry.scratch_paths import ScratchGuard

if TYPE_CHECKING:
    from collections.abc import Iterator

    from quarry.ignore_spec import IgnoreSpec

logger = logging.getLogger(__name__)

_HASH_CHUNK_SIZE: Final[int] = 1 << 20  # 1 MiB


@final
class FileDiscovery:
    """Discover indexable files under a directory, respecting ignore rules."""

    __slots__ = (
        "_directory",
        "_excluded",
        "_guard",
        "_root_resolved",
        "_rules",
        "_walk_complete",
    )

    _directory: Path
    _root_resolved: Path | None
    _guard: ScratchGuard
    _rules: IgnoreRules
    _excluded: bool
    _walk_complete: bool

    def __new__(cls, directory: Path) -> Self:
        """Load ignores and resolve the root, recording resolution failures."""
        self = super().__new__(cls)
        self._directory = directory
        self._guard = ScratchGuard()
        self._rules = IgnoreRules(directory, self._guard)
        try:
            self._root_resolved = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("Cannot resolve registered root: %s", directory)
            self._root_resolved = None
        # A temp/scratch root (OS temp like ``/private/tmp`` OR a repo's own
        # ``.tmp``) is never a document tree: watching it OCR-storms everything
        # dropped there.  Refused up front so both the bulk scan and the live
        # path treat it as empty.
        self._excluded = self._root_resolved is not None and self._guard.refuses_root(
            self._root_resolved
        )
        if self._excluded:
            logger.debug("Skipping scratch/temp root: %s", self._root_resolved)
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
        return self._root_resolved is not None and not self._excluded

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
        logger.warning("Directory walk error under %s: %s", self._directory, exc)
        self._walk_complete = False

    def discover(self, extensions: frozenset[str]) -> list[Path]:
        """Recursively find files matching *extensions* under the directory.

        Respects ``.gitignore`` (at every level), ``.quarryignore``, the
        :class:`~quarry.scratch_paths.ScratchGuard` always-skip names
        (``venv``, ``node_modules``, ``htmlcov``…), and the built-in glob
        defaults (:mod:`quarry.ignore_spec`). Skips dotfiles, macOS resource
        forks (``._*``), and files inside hidden directories (``.Trash``,
        ``.git``, etc.).

        Symlinks whose target resolves outside the directory are dropped and
        logged as a warning.

        Returns absolute paths, sorted for deterministic order.
        """
        self._walk_complete = True
        result: list[Path] = []
        for dirpath, filenames, root_spec, local_spec in self._pruned_walk():
            result.extend(
                self._keep_files(dirpath, filenames, extensions, root_spec, local_spec)
            )
        return result

    def is_watchable_dir(self, dirpath: Path) -> bool:
        """Return whether *dirpath* (a directory under the root) survives pruning.

        Ancestor-aware: a directory reached via watchdog's un-prunable
        backfill walk (``_recursive_simulate``) can be several levels below a
        pruned ancestor whose rejection does not stop that walk from
        descending, so every segment from the root down is checked, exactly
        as :meth:`discover`'s walk would have checked it.
        """
        if self._root_resolved is None or self._excluded:
            return False
        try:
            rel = dirpath.relative_to(self._directory)
        except ValueError:
            return False
        return self._chain_kept(rel)

    def _chain_kept(self, rel_dir: Path) -> bool:
        """Return whether the bulk walk would enter *rel_dir*.

        The per-path twin of :meth:`_pruned_walk`: it applies
        :meth:`~quarry.ignore_spec.IgnoreRules.keeps_dir` to each segment
        from the root down, each against its parent's own ``.gitignore``.
        Every segment must be kept because an excluded directory is never
        entered, so nothing below it can be re-included by a negation.
        """
        root_spec = self._rules.root_spec()
        current = self._directory
        rel_parent = Path()
        for name in rel_dir.parts:
            local_spec = self._rules.local_spec(current)
            if not self._rules.keeps_dir(rel_parent, name, root_spec, local_spec):
                return False
            current /= name
            rel_parent /= name
        return True

    def _pruned_walk(
        self,
    ) -> Iterator[tuple[Path, list[str], IgnoreSpec, IgnoreSpec | None]]:
        """Walk the root, pruning ignored directories in place — the ONE walk seam.

        :meth:`discover` is the sole consumer, descending through this single
        generator so there is exactly one place that decides which
        directories a bulk-scan walk enters.  Yields ``(dirpath, filenames,
        root_spec, local_spec)`` for every directory that survives pruning;
        ``dirnames`` is mutated in place (the documented ``os.walk``
        contract) so a pruned subtree is never entered.
        """
        if self._root_resolved is None or self._excluded:
            return
        root_spec = self._rules.root_spec()
        for dirpath_str, dirnames, filenames in os.walk(
            self._directory, onerror=self._note_walk_error
        ):
            dirpath = Path(dirpath_str)
            local_spec = self._rules.local_spec(dirpath)
            rel_dir = dirpath.relative_to(self._directory)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if self._rules.keeps_dir(rel_dir, name, root_spec, local_spec)
            )
            yield dirpath, filenames, root_spec, local_spec

    def _keep_files(
        self,
        dirpath: Path,
        filenames: list[str],
        extensions: frozenset[str],
        root_spec: IgnoreSpec,
        local_spec: IgnoreSpec | None,
    ) -> Iterator[Path]:
        """Yield the absolute path of each indexable file directly in *dirpath*."""
        rel_dir = dirpath.relative_to(self._directory)
        for filename in sorted(filenames):
            filepath = dirpath / filename
            if filename.startswith((".", "._")):
                continue
            if filepath.suffix.lower() not in extensions:
                continue
            if not self._rules.keeps_file(rel_dir, filename, root_spec, local_spec):
                continue
            if filepath.is_symlink() and not self._symlink_inside_root(filepath):
                continue
            yield filepath.absolute()

    def is_indexable(self, path: Path, extensions: frozenset[str]) -> bool:
        """Return whether *path* would be indexed by :meth:`discover` (live == bulk).

        Applies discover's rules to ONE path so a live watch edit and a bulk scan
        agree: the file's real path must resolve *inside* the real root (a symlink
        whose target escapes the tree is rejected, closing the path-escape leak),
        the suffix must be supported, the file name must not be hidden, every
        ancestor directory must survive the walk's pruning (:meth:`_chain_kept`),
        and the file must survive its directory's ignore rules.
        """
        if self._root_resolved is None or self._excluded:
            return False
        if not self._resolves_inside(path, self._root_resolved):
            return False
        if path.suffix.lower() not in extensions or path.name.startswith("."):
            return False
        try:
            rel = path.relative_to(self._directory)
        except ValueError:
            return False
        if not self._chain_kept(rel.parent):
            return False
        local_spec = self._rules.local_spec(path.parent)
        return self._rules.keeps_file(
            rel.parent, path.name, self._rules.root_spec(), local_spec
        )

    def _hidden_or_skipped(self, rel: Path) -> bool:
        """Whether any segment of *rel* is hidden or a ScratchGuard skip-name.

        Shared by :meth:`is_indexable` and :meth:`is_deletable` (Extract
        Method — both inlined the identical pair of lexical, no-disk-I/O
        checks before this): a hidden/resource-fork prefix or a
        ScratchGuard skip-name, evaluated before either method ever
        touches an ignore SPEC.
        """
        if any(part.startswith((".", "._")) for part in rel.parts):
            return True
        return self._guard.skips_below_root(rel)

    @staticmethod
    def _resolves_inside(path: Path, root: Path) -> bool:
        """Whether *path* resolves inside *root* (symlink-escape guard)."""
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        try:
            resolved.relative_to(root)
        except ValueError:
            return False
        return True

    def is_deletable(self, path: Path, extensions: frozenset[str]) -> bool:
        """Return whether a REMOVED *path* names a document worth a delete job.

        A delete reads no content (no symlink-escape risk) and
        ``delete_document`` is idempotent, so this is a purely lexical check —
        the file is already gone and cannot be resolved.  A false accept is a
        harmless no-op; the disk-vs-registry reconcile is the robust backstop.
        """
        if (
            self._root_resolved is None
            or self._excluded
            or path.suffix.lower() not in extensions
        ):
            return False
        try:
            rel = path.relative_to(self._directory)
        except ValueError:
            return False
        return not self._hidden_or_skipped(rel)

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

    def _symlink_inside_root(self, link: Path) -> bool:
        """Return True iff *link*'s target resolves inside the root."""
        if self._root_resolved is None:
            return False
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("Skipping unresolvable symlink: %s", link)
            return False
        try:
            target.relative_to(self._root_resolved)
        except ValueError:
            logger.warning(
                "Skipping symlink %s that escapes registered root: %s",
                link,
                target,
            )
            return False
        return True
