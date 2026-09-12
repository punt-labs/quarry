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
        backfill walk (``_recursive_simulate``, which reports a newly-created
        subtree's pre-existing contents) can be several levels below a
        pruned ancestor whose own rejection does not stop that walk from
        continuing to list -- and descend into -- its children on disk.  A
        single-level check (only the immediate parent's rules) would wrongly
        accept such a descendant. Every path segment from the root down is
        checked (lexical first, cheapest, no disk I/O): a
        :class:`~quarry.scratch_paths.ScratchGuard` skip-name or a
        hidden-dot prefix (neither cascades to descendants the way a
        ``.gitignore`` DIRECTORY pattern does — gitignore semantics already
        make an ignored directory imply its whole subtree, so the cached
        root spec matched against the FULL relative path gets that for
        free). Only THEN does :meth:`_nested_local_spec_excludes` consult
        each intermediate ancestor's own ``.gitignore`` (:meth:`~quarry.
        ignore_spec.IgnoreRules.local_spec` caches per directory, so this is
        O(depth) with each ancestor's file read at most once per
        :class:`FileDiscovery` instance, not per call) — a burst-created
        ``x/logs/deep`` must be pruned when ``x/.gitignore`` contains
        ``logs/``, even though only ``x``, not ``x/logs/deep``'s own
        immediate parent, names the pattern (Copilot finding, PR #503).
        """
        if self._root_resolved is None or self._excluded:
            return False
        try:
            rel = dirpath.relative_to(self._directory)
        except ValueError:
            return False
        root_spec = self._rules.root_spec()
        if self._ancestor_excluded(rel, root_spec):
            return False
        local_spec = self._rules.local_spec(dirpath.parent)
        return self._rules.keeps_dir(rel.parent, dirpath.name, root_spec, local_spec)

    def _ancestor_excluded(self, rel: Path, root_spec: IgnoreSpec) -> bool:
        """Return whether any ANCESTOR segment of *rel* excludes it.

        The ancestor-aware half of :meth:`is_watchable_dir`'s check, split
        out so that method stays a single-level dispatcher. Cheapest checks
        first: a hidden-dot prefix or a
        :class:`~quarry.scratch_paths.ScratchGuard` skip-name at any
        segment (lexical, no disk I/O), then a root-spec match against the
        FULL relative path (gitignore directory-pattern semantics already
        cascade to descendants, still no disk I/O — the root spec is
        compiled once at construction), and finally each ancestor's own
        NESTED ``.gitignore`` via :meth:`_nested_local_spec_excludes` (one
        cached read per ancestor directory, not re-read per call).
        """
        if any(part.startswith(".") for part in rel.parts):
            return True
        if self._guard.skips_below_root(rel):
            return True
        if self._rules.excludes(root_spec, str(rel), is_dir=True):
            return True
        return self._nested_local_spec_excludes(rel.parts, last_is_dir=True)

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
        whose target escapes the tree is rejected — never indexed, closing the
        path-escape leak), the suffix must be supported, no path segment may be
        hidden, and neither the root nor any per-directory ignore spec may match.
        """
        if self._root_resolved is None or self._excluded:
            return False
        if not self._resolves_inside(path, self._root_resolved):
            return False
        if path.suffix.lower() not in extensions:
            return False
        try:
            rel = path.relative_to(self._directory)
        except ValueError:
            return False
        if self._hidden_or_skipped(rel):
            return False
        if self._rules.excludes(self._rules.root_spec(), str(rel), is_dir=False):
            return False
        return not self._nested_local_spec_excludes(rel.parts, last_is_dir=False)

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

    def _nested_local_spec_excludes(
        self, parts: tuple[str, ...], *, last_is_dir: bool
    ) -> bool:
        """Whether a per-directory ``.gitignore`` along *parts* excludes it.

        Mirrors :meth:`discover`'s walk: each intermediate directory's own
        ``.gitignore`` governs its direct child. Shared by
        :meth:`is_indexable` (*parts* names a FILE — only the final segment
        is a file, ``last_is_dir=False``) and :meth:`_ancestor_excluded`
        (*parts* names a DIRECTORY — every segment, including the last, is
        itself a directory, ``last_is_dir=True``). The root is covered by
        the root ignore spec, so it is skipped here; every other ancestor's
        :meth:`~quarry.ignore_spec.IgnoreRules.local_spec` is read from
        disk at most once per :class:`FileDiscovery` instance (cached
        there per directory), regardless of how many descendants this
        method is later called for.
        """
        current = self._directory
        last = len(parts) - 1
        for index, segment in enumerate(parts):
            if current != self._directory:
                local = self._rules.local_spec(current)
                is_dir = last_is_dir or index != last
                if self._rules.excludes(local, segment, is_dir=is_dir):
                    return True
            current = current / segment
        return False

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
