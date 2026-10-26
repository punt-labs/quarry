"""A private env file, written atomically and durably, its crashed temps swept."""

from __future__ import annotations

import logging
import os
import secrets
import stat
import time
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class EnvFile:
    """Write one env file (it holds the API key) so only its owner can read it.

    The temp sibling has a name of its own (``mkstemp`` semantics), so two
    installs at once never share one, and is created ``O_EXCL`` and
    ``O_NOFOLLOW`` with mode 0600 from the first byte: no chmod race window, and
    never written through a planted symlink.  The file is synced before the
    rename and the directory after it; a failed directory sync is a warning,
    since the file is already in place.

    A crash leaves a temp behind, and it holds the key, so every write first
    sweeps them: the old fixed name and the random ones, only regular files this
    user owns and that are older than :attr:`STALE_AFTER_S` (a live concurrent
    writer holds its temp for milliseconds), and never a symlink.  The config
    directory is tightened to 0700 whether or not it already existed.
    """

    # Long past any live write: a temp this old belongs to a crashed one.
    STALE_AFTER_S: ClassVar[float] = 600.0

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def write(self, lines: list[str]) -> None:
        """Replace the file with *lines*, atomically, durably and privately."""
        directory = self._path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._tighten(directory)
        self._sweep()
        tmp_path = self._path.with_name(f"{self._path.name}.{secrets.token_hex(6)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            f = os.fdopen(fd, "w")
        except BaseException:
            os.close(fd)
            tmp_path.unlink(missing_ok=True)
            raise
        try:
            with f:
                f.write("".join(f"{line}\n" for line in lines))
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self._path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        self._sync(directory)

    @staticmethod
    def _tighten(directory: Path) -> None:
        """Make *directory* owner-only; a directory it cannot change is a warning."""
        try:
            directory.chmod(0o700)
        except OSError as exc:
            logger.warning(
                "could not make %s owner-only (%s)",
                directory,
                FailureReason.os_name(exc),
            )

    def _sweep(self) -> None:
        """Remove the stale temps a crashed write left; say how many, and refusals."""
        name = self._path.name
        removed = refused = 0
        for pattern in (f"{name}.*.tmp", f"{name}.tmp"):
            for leftover in self._path.parent.glob(pattern):
                try:
                    info = leftover.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        refused += 1  # a symlink or anything else: not ours to unlink
                    elif (
                        info.st_uid == os.getuid()
                        and time.time() - info.st_mtime >= self.STALE_AFTER_S
                    ):
                        leftover.unlink()
                        removed += 1
                except OSError as exc:
                    refused += 1
                    logger.debug(
                        "env file: could not sweep a leftover: %s",
                        FailureReason.os_name(exc),
                    )
        if removed or refused:
            logger.warning(
                "env file: swept %d leftover temp file(s), left %d it would not touch",
                removed,
                refused,
            )

    @staticmethod
    def _sync(directory: Path) -> None:
        """Sync *directory*; a failure is a warning, the file is already in place."""
        try:
            handle = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError as exc:
            logger.warning(
                "env file written; directory sync failed (%s); durability not "
                "guaranteed",
                FailureReason.os_name(exc),
            )
