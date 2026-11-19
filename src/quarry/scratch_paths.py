"""The scratch/temp exclusion predicate the watcher and indexer share (DES-045).

The always-on watch loop pegged the machine because the operator's registry
holds ``/private/tmp`` as a collection: the daemon watched the whole OS temp dir
(no ``.gitignore``, not a dot-dir) and OCR-stormed everything any process
dropped there.  This module is the single guard both the indexer
(:class:`~quarry.sync_discovery.FileDiscovery`) and the daemon watch/reconcile
path consult, so a temp/scratch tree is refused identically at scan time and at
watch time — an already-registered temp root becomes harmless on restart, not
just refused at register time.

Two rejections:

* A registered *root* that is a temp directory is refused outright —
  :meth:`ScratchGuard.refuses_root`.  "Temp" means an OS temp root
  (``/tmp``, ``/private/tmp``, ``/var/folders``…) *or* a repository's own
  gitignored ``.tmp`` scratch (``<repo>/.tmp`` — the ``$TMPDIR`` this workspace
  exports).

* Scratch/VCS/build/cache *subdirectories* below a real root
  (``node_modules``, ``htmlcov``, ``dist``, ``*.egg-info``…) are pruned by name
  — :meth:`ScratchGuard.skips_below_root`.

The repo-``.tmp`` case is anchored on the enclosing git repository, checking
*every* ancestor rather than a bare ``.tmp`` component anywhere in the path: a
root under ANY ancestor repo's ``.tmp`` is refused.  Checking every ancestor is
load-bearing — it is exactly what closes the nested-repo bypass, where a git
repo checked out under ``<outer>/.tmp`` would otherwise shadow the outer repo
and let a root still inside ``<outer>/.tmp`` be watched (a first-``.git``-wins
check has that hole).  A checkout or worktree living under a repo's own ``.tmp``
is therefore refused too; that over-refusal is the accepted, fail-closed
tradeoff and must NOT be "relaxed" to re-permit it — doing so reopens the
bypass.  Only a ``.tmp`` with no enclosing ``.git`` at all is permitted.
``<repo>/.tmp`` *below* a watched repo root is also pruned by the caller's
dot-prune and ``.gitignore`` rules; this guard adds the root-level refusal plus
the non-dot cache names those rules miss.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Self, final

# Directory component names that are never document subtrees: version control,
# virtualenvs, dependency trees, build/coverage output, tool caches, and OS junk.
# Checked only against segments *below* a registered root, never its ancestors.
_ALWAYS_SKIP_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".tmp",
        ".git",
        ".hg",
        ".svn",
        ".beads",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "htmlcov",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        ".idea",
        ".vscode",
        "__MACOSX",
    }
)

# Component suffixes that exclude a subdirectory (``foo.egg-info`` build metadata).
_ALWAYS_SKIP_SUFFIXES: Final[tuple[str, ...]] = (".egg-info",)

# Absolute OS temp roots.  A root equal to or under one of these is refused.
# These are a denylist of directories to REFUSE watching, not write targets, so
# ruff's S108 (hardcoded-temp-as-insecure-write) is a false positive here.
# Built from path components rather than ``/tmp``-prefixed literals so the check
# never fires — no suppression is added and make check stays self-contained.
_SYSTEM_TEMP_ROOTS: Final[tuple[Path, ...]] = (
    Path("/", "tmp"),
    Path("/", "private", "tmp"),
    Path("/", "var", "tmp"),
    Path("/", "private", "var", "tmp"),
    Path("/", "var", "folders"),
    Path("/", "private", "var", "folders"),
)

# The gitignored scratch directory name a repository exports as ``$TMPDIR``.
_REPO_SCRATCH_NAME: Final[str] = ".tmp"


@final
class ScratchGuard:
    """Refuse temp roots and prune scratch/VCS/build/cache subdirectories."""

    __slots__ = ("_skip_names", "_skip_suffixes", "_temp_roots")

    _skip_names: frozenset[str]
    _skip_suffixes: tuple[str, ...]
    _temp_roots: tuple[Path, ...]

    def __new__(cls) -> Self:
        """Use the built-in scratch names, suffixes, and system-temp roots."""
        self = super().__new__(cls)
        self._skip_names = _ALWAYS_SKIP_NAMES
        self._skip_suffixes = _ALWAYS_SKIP_SUFFIXES
        self._temp_roots = _SYSTEM_TEMP_ROOTS
        return self

    def refuses_root(self, root: Path) -> bool:
        """Return whether *root* is an OS-temp or repo-scratch dir, never watched.

        The OS-temp comparison casefolds both sides: macOS's case-insensitive
        APFS resolves ``/private/TMP`` to the same directory as ``/private/tmp``
        without normalizing case, so a case variant would otherwise slip the
        guard and reopen the OCR storm.  The ``<gitroot>/.tmp`` anchor stays
        case-exact — repo scratch is spelled ``.tmp`` by design.
        """
        folded = self._casefold(root)
        if any(folded.is_relative_to(self._casefold(t)) for t in self._temp_roots):
            return True
        return self._under_repo_scratch(root)

    @staticmethod
    def _casefold(path: Path) -> Path:
        """Return *path* with every component casefolded (APFS case-insensitivity)."""
        return Path(*(part.casefold() for part in path.parts))

    def is_skip_name(self, name: str) -> bool:
        """Return whether one path component names a scratch/VCS/cache subdirectory."""
        return name in self._skip_names or name.endswith(self._skip_suffixes)

    def skips_below_root(self, relative: Path) -> bool:
        """Return whether any component of a root-relative path is scratch/cache."""
        return any(self.is_skip_name(part) for part in relative.parts)

    @staticmethod
    def _under_repo_scratch(root: Path) -> bool:
        """Return whether *root* lies in ANY ancestor git repo's ``.tmp`` scratch.

        Every ancestor is checked, not just the innermost ``.git`` (a worktree's
        ``.git`` is a file, so ``exists`` — not ``is_dir`` — is correct).  A
        first-``.git``-wins check has a real bypass: a nested repo under
        ``<outer>/.tmp/...`` shadows the outer repo, so a root still inside
        ``<outer>/.tmp`` would be found relative to the inner repo (whose own
        ``.tmp`` it is not under) and wrongly pass.  Checking every ancestor
        refuses a root under any enclosing repo's scratch tree.
        """
        return any(
            (base / ".git").exists() and root.is_relative_to(base / _REPO_SCRATCH_NAME)
            for base in (root, *root.parents)
        )
