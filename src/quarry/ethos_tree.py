"""Locate the ethos sidecar artifacts on disk above a working directory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from quarry.path_guard import FOLLOW_SYMLINKS, PathGuard, SealedTree

if TYPE_CHECKING:
    from collections.abc import Iterator

_PUNT_LABS: Final = Path(".punt-labs")
_ETHOS_DIR: Final = _PUNT_LABS / "ethos"
# Ethos reads the repo pin from ``.punt-labs/ethos.yaml`` and falls back to the
# legacy ``.punt-labs/ethos/config.yaml``; quarry tries both at each ancestor in
# that order so it agrees with ethos on which file wins.
_PIN_FILES: Final = (_PUNT_LABS / "ethos.yaml", _ETHOS_DIR / "config.yaml")
# An ethos handle is a lowercase slug. Hook input (``agent_type``) is checked
# against this before it is ever joined into a path, so ``../claude`` or a
# blank can never become a path segment.
_HANDLE_PATTERN: Final = re.compile(r"^[a-z0-9-]{1,64}$")


@final
class EthosTree:
    """Read-only locator for the ethos artifacts that surround a directory.

    Quarry never imports or calls ethos (the dependency is one-way: ethos
    knows nothing of quarry, and quarry only reads the files ethos leaves on
    disk). Two trees hold identities: the *vendored* one at the nearest
    ``.punt-labs/ethos/`` above ``cwd`` — the layer a ``repo-only`` repo
    resolves from — and the *global* one under the operator's home. Ethos also
    resolves read-only bundles under ``~/.punt-labs/ethos/bundles/``; those are
    deliberately not consulted here, so a bundle-only identity reads as
    absent (a known, stated gap rather than a third path to keep in step).
    """

    __slots__ = ("_start",)

    _start: Path

    def __new__(cls, cwd: str | Path) -> Self:
        self = super().__new__(cls)
        self._start = Path(cwd).resolve()
        return self

    def ancestors(self) -> Iterator[Path]:
        """Yield the start directory, then each parent up to the filesystem root."""
        current = self._start
        while True:
            yield current
            parent = current.parent
            if parent == current:
                return
            current = parent

    def repo_ancestors(self) -> Iterator[Path]:
        """Yield the ancestors inside the checkout that contains the start.

        This is the one bounded walk every per-repo lookup shares — the repo
        pin and the vendored sidecar — so the two cannot disagree about which
        directories belong to "this repo". It is bounded the way ethos bounds
        its own reads: it stops at the first ancestor holding ``.git`` (a
        directory, or a worktree's file) after yielding that root itself, so
        nothing above the checkout — a workspace meta-repo, a parent
        directory — is ever consulted. The ancestor that *is* the operator's
        home is never yielded, even on a walk that passes through it (a
        scratch directory with no repository of its own): ``~/.punt-labs`` is
        the global tree, and reading its pin or its ``ethos/`` as the repo's
        would attribute a session to the operator's global identity and turn
        a global refresh into a phantom working-tree diff. The ancestors are
        resolved paths, so the home is resolved before the comparison: a
        symlinked ``$HOME`` would otherwise never match and the skip would
        silently stop applying.
        """
        home = Path.home().resolve()
        for ancestor in self.ancestors():
            if ancestor != home:
                yield ancestor
            if (ancestor / ".git").exists():
                return

    def pin_files(self) -> Iterator[Path]:
        """Yield every candidate repo-pin path, nearest ancestor first.

        Candidates come only from :meth:`repo_ancestors`: a session is
        attributed to the pin of the checkout it runs in, never to a parent
        directory's or the global tree's. At each ancestor the current
        ``ethos.yaml`` precedes the legacy ``ethos/config.yaml``; the paths are
        candidates, not checked for existence, so the caller decides what an
        absent or malformed file means.
        """
        for ancestor in self.repo_ancestors():
            for pin in _PIN_FILES:
                yield ancestor / pin

    def nearest(self, relative: Path) -> Path | None:
        """Return the closest repo ancestor's *relative* directory, or ``None``.

        ``None`` is the documented "no such sidecar above here" contract — a
        repo without a vendored ethos tree is ordinary, not an error. A
        vendored tree is the one committed in the repository that contains
        ``cwd``, so only :meth:`repo_ancestors` are searched.
        """
        for ancestor in self.repo_ancestors():
            candidate = ancestor / relative
            if candidate.is_dir():
                return candidate
        return None

    def vendored_identities(self) -> Path | None:
        """Return the nearest vendored ``identities/`` directory, or ``None``."""
        return self.nearest(_ETHOS_DIR / "identities")

    def missions_dir(self) -> Path | None:
        """Return the nearest ``.punt-labs/ethos/missions/`` directory, or ``None``."""
        return self.nearest(_ETHOS_DIR / "missions")

    def identities_dirs(self) -> tuple[Path, ...]:
        """Return the identity trees to consult: vendored first, then global.

        Only directories that exist are returned, so a caller can iterate
        without re-checking presence.
        """
        return tuple(path for path, _guard in self.identity_trees())

    def identity_trees(self) -> tuple[tuple[Path, PathGuard], ...]:
        """Return each existing identity tree with the guard its files are read under.

        The vendored tree is cloned content and is sealed at its checkout
        root: an identity reached through a symlink is not the repo's. The
        global tree is the operator's own and follows symlinks (dotfile
        managers put them there on purpose).
        """
        trees: list[tuple[Path, PathGuard]] = []
        vendored = self.vendored_identities()
        if vendored is not None:
            trees.append((vendored, SealedTree(self.checkout_root(vendored))))
        global_tree = self.global_identities()
        if global_tree.is_dir():
            trees.append((global_tree, FOLLOW_SYMLINKS))
        return tuple(trees)

    def identity_exists(self, handle: str) -> bool:
        """Return whether *handle* names a registered identity in either tree.

        The handle is validated as a slug before any path is built; an
        invalid handle is simply "not an identity", never a filesystem probe.
        A vendored ``<handle>.yaml`` that is a symlink does not count: the
        file it points at is wherever the committer chose, and attribution
        must not follow it.
        """
        if not self.is_valid_handle(handle):
            return False
        return any(
            guard.is_regular_file(identities / f"{handle}.yaml")
            for identities, guard in self.identity_trees()
        )

    @classmethod
    def read_sidecar(cls, path: Path) -> str:
        """Read a file under a ``.punt-labs`` sidecar, sealed at its checkout root.

        The sidecar is cloned content: a repo pin, a vendored identity, or a
        mission's YAML that is — or sits below — a symlink is refused inside
        the open (:class:`~quarry.path_guard.SealedTree`), never followed to
        whatever file the committer pointed it at, such as the operator's
        global pin. Absence propagates as ``FileNotFoundError``; a refusal is
        a :class:`~quarry.path_guard.SealedTreeError`, an ``OSError``.
        """
        return SealedTree(cls.checkout_root(path)).read_text(path)

    @staticmethod
    def checkout_root(sidecar: Path) -> Path:
        """Return the checkout that vendors *sidecar* — the parent of ``.punt-labs``.

        ``<repo>/.punt-labs/ethos/<name>`` -> ``<repo>``, and likewise for a
        pin (``<repo>/.punt-labs/ethos.yaml``) or a file deeper in the tree.
        The checkout is the trust boundary for everything under the sidecar:
        the operator chose the checkout, but its contents are cloned and may
        hold a symlink. A path with no ``.punt-labs`` component is a caller
        bug, not a sidecar, and raises ``ValueError``.
        """
        for parent in sidecar.parents:
            if parent.name == _PUNT_LABS.name:
                return parent.parent
        msg = f"{sidecar} is not under a {_PUNT_LABS} sidecar"
        raise ValueError(msg)

    @staticmethod
    def is_valid_handle(handle: str) -> bool:
        """Return whether *handle* is a lowercase slug safe to use as a path leaf."""
        return _HANDLE_PATTERN.fullmatch(handle) is not None

    @staticmethod
    def global_identities() -> Path:
        """Return the operator's global identities directory (may not exist).

        Read from ``Path.home()`` at call time, not bound at import, so the
        hermetic test HOME redirect applies.
        """
        return Path.home() / _ETHOS_DIR / "identities"
