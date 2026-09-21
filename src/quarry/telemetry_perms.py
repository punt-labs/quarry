"""Harden the recall-telemetry SQLite path's permissions before sqlite touches it.

Split out of ``query_log.py`` (PY-IC-6 single responsibility): "make this path
private" is one cohesive, reusable concern, distinct from owning the
connection, recording rows, and scheduling prunes that ``QueryLog`` does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Self, final


@final
class TelemetryPathGuard:
    """Create a telemetry SQLite path's directory 0700 and leaf 0600.

    The telemetry store holds 90 days of scrubbed queries -- ``serve.token``
    is mode-0600 so another uid on this host cannot call ``/insights``, and
    this file must be no more permissive. ``sqlite3.connect`` creates a
    brand-new database with umask-masked ``0666`` permissions (typically
    ``0644``), so both the parent directory and the leaf are secured here,
    *before* sqlite ever touches the path: it only ever opens an
    already-private file. sqlite mirrors the main file's mode onto the
    WAL/SHM sidecars it creates alongside it, so securing the leaf secures
    all three.
    """

    _DIR_MODE: Final = 0o700
    _DB_MODE: Final = 0o600

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def secure(self) -> None:
        """Create the directory and leaf if absent; tighten either if looser."""
        self._secure_directory(self._path.parent)
        self._secure_file(self._path)

    @classmethod
    def _secure_directory(cls, directory: Path) -> None:
        """Create *directory* ``0o700``, tightening it if it already exists looser.

        Mirrors ``JobSpool._ensure_private_dir`` -- the parent of a file
        holding 90 days of scrubbed queries must never be more permissive
        than the leaf itself.
        """
        directory.mkdir(parents=True, exist_ok=True, mode=cls._DIR_MODE)
        directory.chmod(cls._DIR_MODE)

    @classmethod
    def _secure_file(cls, path: Path) -> None:
        """Create *path* mode-0600 if absent; tighten it in place if looser.

        Pre-creating the empty leaf with ``os.open(..., 0o600)`` avoids a
        create-then-chmod race window; an existing looser file (an upgrade
        from an earlier release) is tightened rather than left loose.
        """
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags, cls._DB_MODE)
        except FileExistsError:
            path.chmod(cls._DB_MODE)
        else:
            os.close(fd)
