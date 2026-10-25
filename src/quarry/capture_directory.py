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

        The caller closes it.

        Raises:
            CaptureSymlinkError: A level below the project root is a symlink.
            FileNotFoundError: A level is missing and *create* is false.
        """
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        for part in self.RELATIVE:
            try:
                child = self._descend(fd, part, create=create)
            finally:
                os.close(fd)
            fd = child
        return fd

    @staticmethod
    def _descend(parent: int, name: str, *, create: bool) -> int:
        """Return a descriptor for *name* under *parent*, never through a symlink."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            return os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            with contextlib.suppress(FileExistsError):  # a writer beside us made it
                os.mkdir(name, 0o755, dir_fd=parent)
            return os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno in _REFUSED:
                msg = f"{name} is a symlink or not a directory"
                raise CaptureSymlinkError(exc.errno, msg) from exc
            raise
