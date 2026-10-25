"""A project's captures directory, opened without following a symlink at any level."""

from __future__ import annotations

import contextlib
import errno
import os
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_REFUSED = frozenset({errno.ELOOP, errno.ENOTDIR})


class CaptureSymlinkError(OSError):
    """A level of the captures path is a symlink, or not a directory."""


@final
class CaptureDirectory:
    """The captures directory under a project root, reached one level at a time.

    The project root itself may be reached through a symlink (a repository
    checked out through one is ordinary).  Every level below it is opened with
    ``O_NOFOLLOW`` relative to the level above, so a symlink at any of them is
    refused, and one planted after a check cannot be followed: there is no
    check, only the open.  The returned descriptor is the directory itself, so
    everything done through it stays inside it.
    """

    RELATIVE: ClassVar[tuple[str, ...]] = (".punt-labs", "quarry", "captures")

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @contextlib.contextmanager
    def opened(self, *, create: bool) -> Generator[int]:
        """Hold a descriptor for the directory for the length of the block."""
        fd = self.open(create=create)
        try:
            yield fd
        finally:
            os.close(fd)

    def open(self, *, create: bool) -> int:
        """Return a descriptor for the directory, creating it if *create*.

        The caller closes it.  The captures directory itself is created
        owner-only (0700); the levels above it, which hold other state, are 0755.

        Raises:
            CaptureSymlinkError: A level below the project root is a symlink, or
                not a directory.
            FileNotFoundError: A level is missing and *create* is false.
        """
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        for index, part in enumerate(self.RELATIVE):
            mode = 0o700 if index == len(self.RELATIVE) - 1 else 0o755
            try:
                child = self._descend(fd, part, mode, create=create)
            finally:
                os.close(fd)
            fd = child
        return fd

    @classmethod
    def _descend(cls, parent: int, name: str, mode: int, *, create: bool) -> int:
        """Return a descriptor for *name* under *parent*, never through a symlink."""
        try:
            return cls._open_at(parent, name)
        except FileNotFoundError:
            if not create:
                raise
        with contextlib.suppress(FileExistsError):  # a writer beside us made it
            os.mkdir(name, mode, dir_fd=parent)
        return cls._open_at(parent, name)  # refused the same way, whoever made it

    @staticmethod
    def _open_at(parent: int, name: str) -> int:
        """Open the directory *name* under *parent* without following a symlink."""
        try:
            return os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
        except OSError as exc:
            if exc.errno not in _REFUSED:
                raise
            what = "a symlink" if exc.errno == errno.ELOOP else "a non-directory"
            raise CaptureSymlinkError(
                exc.errno, f"{name} is {what} in the path"
            ) from exc
