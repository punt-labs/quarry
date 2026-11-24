"""A project's captures directory, opened without following a symlink at any level."""

from __future__ import annotations

import contextlib
import errno
import os
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.checkout_ignore import CheckoutIgnore

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

    RELATIVE: ClassVar[tuple[str, ...]] = (".punt-labs", "local", "quarry", "captures")
    RELATIVE_PATH: ClassVar[str] = "/".join(RELATIVE)
    LEGACY: ClassVar[str] = ".punt-labs/quarry/captures"
    IGNORE: ClassVar[str] = RELATIVE_PATH + "/"
    IGNORE_PATTERNS: ClassVar[tuple[str, str]] = (IGNORE, LEGACY + "/")

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        """Anchor capture-directory traversal at the supplied project root."""
        self = super().__new__(cls)
        self._root = root.resolve()
        return self

    @classmethod
    def path_at(cls, root: Path) -> Path:
        """Return the project's private captures path (PLD-2)."""
        return root.joinpath(*cls.RELATIVE)

    def is_ignored(self, name: str, *, include_parents: bool = False) -> bool | None:
        """Check the directory and target; return None when not in Git."""
        return CheckoutIgnore.covers(
            self._root,
            (self.IGNORE, f"{self.IGNORE}{name}"),
            include_parents=include_parents,
        )

    @contextlib.contextmanager
    def opened_for_write(self, name: str) -> Generator[int]:
        """Open safely and refuse writes unless the checkout excludes this target."""
        ignored = self.is_ignored(name, include_parents=True)
        with self.opened(create=ignored is not False, protect=ignored is None) as fd:
            if ignored is False:
                msg = f"protect {self.IGNORE} in .gitignore"
                raise PermissionError(msg)
            yield fd

    @contextlib.contextmanager
    def opened(self, *, create: bool, protect: bool = False) -> Generator[int]:
        """Hold a descriptor for the directory for the length of the block."""
        fd = self.open(create=create, protect=protect)
        try:
            yield fd
        finally:
            os.close(fd)

    def open(self, *, create: bool, protect: bool = False) -> int:
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
            is_leaf = index == len(self.RELATIVE) - 1
            try:
                if is_leaf and protect:
                    CheckoutIgnore.protect_future_checkout(fd, part)
                child = self._descend(
                    fd, part, 0o700 if is_leaf else 0o755, create=create
                )
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
