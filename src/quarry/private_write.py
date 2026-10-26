"""Put a text file in place owner-only, synced, and atomically."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Self, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class PrivateWrite:
    """Write a file that holds something private so it is never half-there.

    The temp file is created ``O_EXCL`` and ``O_NOFOLLOW`` with mode 0600 from
    its first byte: no chmod race window, and never written through a planted
    symlink.  It is synced before the rename and the directory after it, so the
    final file survives a crash.  The temp is removed on any failure, and
    ``os.fdopen`` raising closes the descriptor it never adopted.  A failed
    directory sync is a warning naming *label*, since the file is already in
    place.
    """

    __slots__ = ("_label",)

    _label: str

    def __new__(cls, label: str) -> Self:
        self = super().__new__(cls)
        self._label = label
        return self

    def commit(self, tmp: Path, final_path: Path, text: str) -> None:
        """Write *text* to *tmp*, then rename it over *final_path*."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            self._discard(tmp)
            raise
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(final_path)
        except BaseException:
            self._discard(tmp)
            raise
        self.sync_directory(final_path.parent, f"{self._label} written")

    def _discard(self, tmp: Path) -> None:
        """Remove *tmp*; a failure to is a warning, never the error that surfaces.

        A temp left behind can hold the very secret this class exists to protect,
        so the failure is named (by errno) rather than swallowed.
        """
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "%s: could not remove its temp file %s (%s)",
                self._label,
                tmp,
                FailureReason.os_name(exc),
            )

    @classmethod
    def ensure_directory(cls, directory: Path, what: str) -> None:
        """Create *directory* ``0o700`` (tightening one that exists looser).

        Only *directory* itself gets that mode: a parent this call has to create
        gets the process's default, so a private tree needs its root made first.

        Every level this call creates is synced into its parent, top-down, or
        files written into it could outlive the directory's own name in a crash;
        *what* names the directory in a warning when a sync fails.
        """
        created = [
            d for d in (*reversed(directory.parents), directory) if not d.exists()
        ]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        for level in created:
            cls.sync_directory(level.parent, f"{what} {level.name} created")

    @staticmethod
    def sync_directory(directory: Path, what: str) -> None:
        """Sync *directory*; a failure is a warning, *what* is already in place."""
        try:
            handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError as exc:
            logger.warning(
                "%s; directory sync failed (%s); durability not guaranteed",
                what,
                FailureReason.os_name(exc),
            )
