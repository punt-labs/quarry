"""A private env file, written atomically and durably, its crashed temps swept."""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Self, final

from quarry.failure_reason import FailureReason
from quarry.private_write import PrivateWrite
from quarry.temp_sweep import TempSweep

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
    sweeps them (see :class:`TempSweep`).  The config directory is tightened to
    0700 whether or not it already existed.
    """

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
        TempSweep.beside(self._path, "env file").run()
        tmp_path = self._path.with_name(f"{self._path.name}.{secrets.token_hex(6)}.tmp")
        PrivateWrite("env file").commit(
            tmp_path, self._path, "".join(f"{line}\n" for line in lines)
        )

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
