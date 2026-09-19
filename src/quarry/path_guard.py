"""Refuse paths that reach their target through a symlink below an untrusted root.

A cloned repository's sidecar files (``.punt-labs/ethos/…``) are attacker
content: a symlink committed there can point a read at, or a write onto, any
file the operator can reach — another checkout's mission YAML, the operator's
global identity ext. The checkout root itself is trusted (the operator chose
it), so the rule is lexical containment under the root plus no symlink at any
component *below* it. Operator-owned trees (the global ``~/.punt-labs/ethos/``)
keep following symlinks: dotfile managers put them there on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, Self, final

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
    """Decide whether a path may be read from or written to."""

    def check(self, path: Path) -> Path:
        """Return *path* when it may be used; raise :class:`SealedTreeError` if not."""
        ...


@final
class FollowSymlinks:
    """The trusting guard: every path is the operator's own, symlinks included."""

    __slots__ = ()

    def check(self, path: Path) -> Path:
        """Return *path* unchanged; nothing is refused."""
        return path


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
        decides what an absent file means.
        """
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            msg = f"{path} is outside {self._root}; refused"
            raise SealedTreeError(msg) from None
        current = self._root
        for part in relative.parts:
            if part == "..":
                msg = f"{path} escapes {self._root}; refused"
                raise SealedTreeError(msg)
            current = current / part
            if current.is_symlink():
                msg = f"{current} is a symlink under {self._root}; refused"
                raise SealedTreeError(msg)
        return path


# The default guard for callers that have not named a trust boundary.
FOLLOW_SYMLINKS: Final = FollowSymlinks()
