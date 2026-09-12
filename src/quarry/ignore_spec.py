"""The gitignore-dialect ignore/prune rules shared by discovery and watch scheduling.

Both a bulk directory scan (:class:`~quarry.sync_discovery.FileDiscovery`) and
the live Linux watch scheduler (:mod:`~quarry.daemon.inotify_prune`, by way of
``FileDiscovery.is_watchable_dir``) must agree on which directories and files
are ignored — ``.gitignore`` at every level,
``.quarryignore`` at the root, the built-in scratch defaults, and hidden-dir
skipping.  :class:`FileDiscovery` composes ONE :class:`IgnoreRules` per
registered root rather than re-reading ignore files and re-matching glob
patterns itself, so there is exactly one place that owns "is this name
ignored" — pulled out of :mod:`~quarry.sync_discovery` (Extract Class) once
that module's own concerns (walking, hashing, per-file live checks) started
crowding the ignore-spec bookkeeping out.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final, Self, final

import pathspec

from quarry.gitignore import CAPTURES_GITIGNORE_ENTRY
from quarry.scratch_paths import ScratchGuard

logger = logging.getLogger(__name__)

type IgnoreSpec = pathspec.PathSpec[pathspec.pattern.Pattern]

# Glob-style ignores the pathspec engine handles.  Scratch/VCS/build/cache
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
    CAPTURES_GITIGNORE_ENTRY,
]


@final
class IgnoreRules:
    """Load and apply one directory tree's ignore spec (gitignore dialect).

    ``root_spec`` is computed ONCE, at construction, and cached for the
    instance's lifetime: the observer-thread hot path (a live watch's
    auto-add for a newly created directory) must never re-read
    ``.gitignore``/``.quarryignore`` from disk per event.  ``local_spec`` is
    cached per directory the first time it is asked for, since the set of
    directories a live watch or a bulk scan visits is not known up front.
    Both caches are scoped to one ``IgnoreRules`` instance — one per
    registered root per watch/scan session — so a change to an ignore file
    is picked up on the NEXT session (daemon restart or re-registration),
    not mid-session; this matches "the SAME loaded spec" every consumer of
    this class already assumes.
    """

    __slots__ = ("_directory", "_guard", "_local_spec_cache", "_root_spec")

    _directory: Path
    _guard: ScratchGuard
    _root_spec: IgnoreSpec
    _local_spec_cache: dict[Path, IgnoreSpec | None]

    def __new__(cls, directory: Path, guard: ScratchGuard) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._guard = guard
        self._root_spec = self._build_root_spec()
        self._local_spec_cache = {}
        return self

    def root_spec(self) -> IgnoreSpec:
        """Return the cached ``.gitignore``/``.quarryignore``/defaults PathSpec."""
        return self._root_spec

    def local_spec(self, dirpath: Path) -> IgnoreSpec | None:
        """Return *dirpath*'s own ``.gitignore`` as a spec, or ``None`` (cached).

        ``None`` covers both "the tree root itself" (its ``.gitignore``
        already folds into :meth:`root_spec`) and "no per-directory override
        file present" — a caller need not distinguish the two.
        """
        if dirpath == self._directory:
            return None
        if dirpath not in self._local_spec_cache:
            lines = self._read_ignore_lines(dirpath / ".gitignore")
            self._local_spec_cache[dirpath] = (
                self._compile_lines(lines) if lines else None
            )
        return self._local_spec_cache[dirpath]

    def keeps_dir(
        self,
        rel_dir: Path,
        name: str,
        root_spec: IgnoreSpec,
        local_spec: IgnoreSpec | None,
    ) -> bool:
        """Return whether child directory *name* of *rel_dir* survives pruning.

        The single predicate every consumer of the ignore rules applies to a
        directory — the bulk walk's directory filter, the watch scheduler's
        single-directory check, and the pruned inotify walk all call this,
        never their own copy of the same four conditions.
        """
        return (
            not name.startswith(".")
            and not self._guard.is_skip_name(name)
            and not self.excludes(root_spec, str(rel_dir / name), is_dir=True)
            and not self.excludes(local_spec, name, is_dir=True)
        )

    def keeps_file(
        self,
        rel_dir: Path,
        name: str,
        root_spec: IgnoreSpec,
        local_spec: IgnoreSpec | None,
    ) -> bool:
        """Return whether file *name* in *rel_dir* survives pruning.

        Hidden-name/resource-fork exclusion is deliberately NOT applied here
        (unlike :meth:`keeps_dir`): ``discover()``'s bulk walk and
        ``is_indexable``'s live check differ slightly on which dotfile
        prefixes they reject, so that decision stays with the caller — this
        method owns only the ignore-SPEC matching, the one piece every
        caller must agree on.
        """
        rel_path = str(rel_dir / name) if str(rel_dir) != "." else name
        return not self.excludes(
            root_spec, rel_path, is_dir=False
        ) and not self.excludes(local_spec, name, is_dir=False)

    @staticmethod
    def excludes(spec: IgnoreSpec | None, name: str, *, is_dir: bool) -> bool:
        """Return whether *spec* matches *name* (a directory gets a trailing ``/``).

        The ONE place a ``pathspec.PathSpec.match_file`` call is made —
        every caller in this module and in
        :class:`~quarry.sync_discovery.FileDiscovery` routes through this
        rather than calling ``match_file`` directly, so there is exactly one
        place that knows gitignore's directory-vs-file matching convention.
        """
        if spec is None:
            return False
        return spec.match_file(name + "/" if is_dir else name)

    def _build_root_spec(self) -> IgnoreSpec:
        """Build a PathSpec from ``.gitignore``, ``.quarryignore``, and defaults."""
        lines: list[str] = list(_DEFAULT_IGNORE_PATTERNS)
        for name in (".gitignore", ".quarryignore"):
            lines.extend(self._read_ignore_lines(self._directory / name))
        return self._compile_lines(lines)

    @staticmethod
    def _compile_lines(lines: list[str]) -> IgnoreSpec:
        """Compile *lines* into a PathSpec, skipping any invalid pattern.

        A malformed gitignore line (a bare ``!``, a trailing unescaped
        ``\\``) raises ``pathspec``'s ``GitIgnorePatternError`` — a
        ``ValueError``, not an ``OSError`` — from the pattern factory
        itself, not from any file I/O :meth:`_read_ignore_lines` already
        guards.  Left to propagate, this crashes ``IgnoreRules``
        construction: at daemon-register time
        (:meth:`~quarry.daemon.fs_watchdog.WatchdogSource.schedule`, whose
        ``except OSError`` does not catch it) or, worse, on the watchdog
        buffer thread when a NESTED ``.gitignore`` written mid-session is
        lazily compiled by :meth:`~quarry.daemon.inotify_prune.
        PrunedInotify._add_watch` (whose callers, all inherited from
        vanilla watchdog, also tolerate only ``OSError``) — permanently
        killing that thread while ``watch_state`` keeps reporting
        "watched".  Compiling line by line and skipping the bad one
        (logged, not silent) keeps every OTHER pattern in the file
        working instead of losing the whole spec, or the whole daemon, to
        one typo.
        """
        factory = pathspec.lookup_pattern("gitignore")
        patterns: list[pathspec.pattern.Pattern] = []
        for line in lines:
            if not line:
                continue
            try:
                patterns.append(factory(line))
            except ValueError as exc:
                logger.warning("Skipping invalid ignore pattern %r: %s", line, exc)
        return pathspec.PathSpec(patterns)

    @staticmethod
    def _read_ignore_lines(path: Path) -> list[str]:
        """Return an ignore file's lines, or ``[]`` when absent/non-regular/unreadable.

        ``is_file()`` inside the ``try`` (not before it) keeps a FIFO or a symlink
        to a character device named ``.gitignore`` from blocking ``read_text()``
        forever, while the ``OSError`` guard keeps a raced deletion — present at
        the check, gone at the read — from aborting the whole caller's walk
        (bug class 1/2).  ``UnicodeDecodeError`` (a non-UTF-8 ignore file) is a
        ``ValueError``, not an ``OSError`` — caught here for the same reason.
        """
        try:
            if not path.is_file():
                return []
            return path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Skipping unreadable ignore file %s: %s", path, exc)
            return []
        except UnicodeDecodeError as exc:
            logger.warning("Skipping non-UTF-8 ignore file %s: %s", path, exc)
            return []
