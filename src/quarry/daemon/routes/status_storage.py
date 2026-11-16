"""The size and free-disk figures of ``/status``: bytes, measured, never row data."""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING, Self, final

from quarry.db.table_probe import StorageSizes, TableProbe

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class StatusStorage:
    """On-disk bytes, live bytes, and free disk for one database directory.

    A figure that cannot be measured is reported as ``0`` rather than failing the
    whole status call: status must stay readable on exactly the machines whose
    disk is in trouble.
    """

    __slots__ = ("_db_path",)

    _db_path: Path

    def __new__(cls, db_path: Path) -> Self:
        self = super().__new__(cls)
        self._db_path = db_path
        return self

    def fields(self) -> dict[str, int]:
        """Return the ``disk_bytes``, ``live_bytes`` and ``free_disk_bytes`` fields."""
        sizes = self._sizes()
        return {
            "disk_bytes": sizes.disk_bytes,
            "live_bytes": sizes.live_bytes,
            "free_disk_bytes": self._free(),
        }

    def _sizes(self) -> StorageSizes:
        try:
            return TableProbe.sizes(self._db_path)
        except (OSError, ValueError, RuntimeError):
            logger.warning("status: could not measure the table size", exc_info=True)
            return StorageSizes(disk_bytes=0, live_bytes=0)

    def _free(self) -> int:
        try:
            return shutil.disk_usage(self._db_path).free
        except OSError:
            logger.warning("status: could not read free disk space", exc_info=True)
            return 0
