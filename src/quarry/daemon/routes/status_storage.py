"""The size and free-disk figures of ``/status``: bytes, measured, never row data."""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING, Self, final

from quarry.db.table_probe import StorageSizes, TableProbe

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@final
class StatusStorage:
    """On-disk bytes, live bytes, and free disk for one database directory.

    A figure that cannot be measured is reported as ``None`` rather than failing
    the whole status call: status must stay readable on exactly the machines whose
    disk is in trouble, and ``None`` keeps a failed measurement distinct from a
    measured zero.  ``quarry doctor`` is the authoritative gate and fails loudly.
    """

    __slots__ = ("_db", "_db_path")

    _db_path: Path
    _db: LanceDB

    def __new__(cls, db_path: Path, db: LanceDB) -> Self:
        """Pair an open database with its directory for disk-usage readings."""
        self = super().__new__(cls)
        self._db_path = db_path
        self._db = db
        return self

    def fields(self) -> dict[str, int | None]:
        """Return the ``disk_bytes``, ``live_bytes`` and ``free_disk_bytes`` fields."""
        sizes = self._sizes()
        return {
            "disk_bytes": sizes.disk_bytes if sizes else None,
            "live_bytes": sizes.live_bytes if sizes else None,
            "free_disk_bytes": self._free(),
        }

    def _sizes(self) -> StorageSizes | None:
        # None: the table could not be measured; not an empty table.
        try:
            return TableProbe.sizes(self._db_path, self._db)
        except (OSError, ValueError, RuntimeError):
            logger.warning("status: could not measure the table size", exc_info=True)
            return None

    def _free(self) -> int | None:
        # None: free space could not be read; not a full disk.
        try:
            return shutil.disk_usage(self._db_path).free
        except OSError:
            logger.warning("status: could not read free disk space", exc_info=True)
            return None
