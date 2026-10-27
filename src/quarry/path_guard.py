"""Read and rewrite repo-controlled sidecar files without following a symlink out.

A cloned repository's sidecar files (``.punt-labs/ethos/…``) are attacker
content: a symlink committed there can point a read at any file the operator
can reach — the operator's global identity pin, another checkout's mission
YAML — or point a write (the ext-guide refresh) at a file outside the repo.
The checkout root itself is trusted (the operator chose it), so the rule is
lexical containment under the root plus no symlink at any component *below*
it. The refusal lives in the read or write itself (an ``openat`` walk with
``O_NOFOLLOW`` on every component, via :class:`~quarry.safe_paths.SafeRepoPath`),
not in a check made before it, so a component swapped for a link between a
check and the open is refused all the same. Operator-owned trees (the global
``~/.punt-labs/ethos/``) keep following symlinks: dotfile managers put them
there on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, Self, final

from quarry.atomic_file import AtomicFile
from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "FOLLOW_SYMLINKS",
    "FollowSymlinks",
    "PathGuard",
    "SealedTree",
    "SealedTreeError",
]


class SealedTreeError(OSError):
    """A path leaves its sealed tree — by ``..``, another prefix, or a symlink."""


class PathGuard(Protocol):
    """Read or rewrite a repo-controlled file, deciding whether to follow a symlink."""

    def check(self, path: Path) -> Path:
        """Return *path* when it may be used; raise :class:`SealedTreeError` if not.

        A pre-filter for directory listings, not the security boundary: the
        boundary is :meth:`read_text` / :meth:`write_text`, which decide
        inside the open.
        """
        ...

    def read_text(self, path: Path) -> str:
        """Return the file's text; raise :class:`SealedTreeError` on a refused link."""
        ...

    def write_text(self, path: Path, text: str) -> None:
        """Replace the file's text atomically, keeping its mode.

        Raise :class:`SealedTreeError` on a refused link.
        """
        ...

    def is_regular_file(self, path: Path) -> bool:
        """Return whether a regular file that :meth:`read_text` would read is here."""
        ...


@final
class FollowSymlinks:
    """The trusting guard: every path is the operator's own, symlinks included."""

    __slots__ = ()

    def check(self, path: Path) -> Path:
        """Return *path* unchanged; nothing is refused."""
        return path

    def read_text(self, path: Path) -> str:
        """Read *path*, following any symlink on the way."""
        return path.read_text(encoding="utf-8", newline="")

    def write_text(self, path: Path, text: str) -> None:
        """Rewrite *path* atomically through any symlink, keeping link and mode."""
        AtomicFile(path).replace(text)

    def is_regular_file(self, path: Path) -> bool:
        """Return whether *path* (through any symlink) is a regular file."""
        return path.is_file()


@final
class SealedTree:
    """A tree in which every component below *root* must be a real file or directory."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def root(self) -> Path:
        """Return the trusted root; the root itself may be a symlink."""
        return self._root

    def check(self, path: Path) -> Path:
        """Return *path* if it sits under the root with no symlink on the way.

        Containment is lexical (``relative_to``), so ``..`` and a path under
        another prefix are refused before the filesystem is touched; each
        component below the root is then ``lstat``-ed in turn. A component
        that does not exist yet is not a symlink and passes — the caller
        decides what an absent file means. This is a pre-filter (a directory
        listing skips a linked entry with a warning); every read and write
        re-walks inside :meth:`read_text` / :meth:`write_text`.
        """
        current = self._root
        for part in self._contained(path):
            current = current / part
            if current.is_symlink():
                msg = f"{current} is a symlink under {self._root}; refused"
                raise SealedTreeError(msg)
        return path

    def read_text(self, path: Path) -> str:
        """Read *path* with ``O_NOFOLLOW`` on every component below the root.

        A symlink anywhere below the root — leaf or directory, present at the
        time of the open — is refused with :class:`SealedTreeError`. Absence
        propagates as ``FileNotFoundError``, so a caller can tell "not here"
        from "refused".
        """
        try:
            return self._sealed(path).read_text()
        except ValueError as exc:
            raise SealedTreeError(f"{exc}; refused") from exc

    def write_text(self, path: Path, text: str) -> None:
        """Rewrite *path* atomically, ``O_NOFOLLOW`` on every component below the root.

        A symlinked ancestor is refused inside the ``openat`` walk; a symlinked
        (or otherwise non-regular) leaf is refused before a temp file is made;
        and a link swapped in after that is only ever replaced as a directory
        entry, never written through (``rename(2)`` does not follow its
        destination). The leaf keeps its mode. Every refusal is a
        :class:`SealedTreeError`; other I/O failures propagate as they are.
        """
        try:
            self._sealed(path).write_atomic(text)
        except ValueError as exc:
            raise SealedTreeError(f"{exc}; refused") from exc

    def is_regular_file(self, path: Path) -> bool:
        """Return whether *path* is a regular file reached through no symlink.

        A symlinked leaf or ancestor reads as absent: the file it points at is
        not the repo's, so the repo does not have it.
        """
        return self._sealed(path).is_regular_file()

    def _sealed(self, path: Path) -> SafeRepoPath:
        """Return the ``openat``-walked handle for *path*, contained lexically first."""
        parts = self._contained(path)
        if not parts:
            msg = f"{path} is the sealed root itself, not a file under it; refused"
            raise SealedTreeError(msg)
        return SafeRepoPath(self._root, parts)

    def _contained(self, path: Path) -> tuple[str, ...]:
        """Return *path*'s components below the root, refusing ``..`` and escape."""
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            msg = f"{path} is outside {self._root}; refused"
            raise SealedTreeError(msg) from None
        if ".." in relative.parts:
            msg = f"{path} escapes {self._root}; refused"
            raise SealedTreeError(msg)
        return relative.parts


# The default guard for callers that have not named a trust boundary.
FOLLOW_SYMLINKS: Final = FollowSymlinks()
