"""Locate the ethos sidecar artifacts on disk above a working directory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterator

_ETHOS_DIR: Final = Path(".punt-labs") / "ethos"
# Ethos reads the repo pin from ``.punt-labs/ethos.yaml`` and falls back to the
# legacy ``.punt-labs/ethos/config.yaml``; quarry tries both at each ancestor in
# that order so it agrees with ethos on which file wins.
_PIN_FILES: Final = (Path(".punt-labs") / "ethos.yaml", _ETHOS_DIR / "config.yaml")
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
        a global refresh into a phantom working-tree diff.
        """
        home = Path.home()
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
        candidates = (self.vendored_identities(), self.global_identities())
        return tuple(path for path in candidates if path is not None and path.is_dir())

    def identity_exists(self, handle: str) -> bool:
        """Return whether *handle* names a registered identity in either tree.

        The handle is validated as a slug before any path is built; an
        invalid handle is simply "not an identity", never a filesystem probe.
        """
        if not self.is_valid_handle(handle):
            return False
        return any(
            (identities / f"{handle}.yaml").is_file()
            for identities in self.identities_dirs()
        )

    @staticmethod
    def checkout_root(sidecar: Path) -> Path:
        """Return the checkout that vendors *sidecar*.

        ``<repo>/.punt-labs/ethos/<name>`` -> ``<repo>``. The checkout is the
        trust boundary for everything under the sidecar: the operator chose
        the checkout, but its contents are cloned and may hold a symlink.
        """
        return sidecar.parents[2]

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
