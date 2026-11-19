"""Scan a table in batches that shrink when a read overflows."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterator

    from quarry.types import LanceTable

logger = logging.getLogger(__name__)


@final
class AdaptiveScan:
    """Iterate a table's rows in batches, halving the batch size on a failed read.

    A table holding multi-megabyte rows can overflow Arrow's 32-bit string
    offsets when a batch spans too many of them.  The scan starts at *start*
    rows, halves on a failure down to one row, and raises only when a single row
    cannot be read.  It never grows the batch back: a poisoned region is
    usually followed by more of the same.
    """

    __slots__ = ("_columns", "_start", "_table")

    _table: LanceTable
    _columns: list[str]
    _start: int

    def __new__(cls, table: LanceTable, columns: list[str], *, start: int) -> Self:
        """Select scan columns and a positive initial batch size.

        Args:
            start: Initial rows per read; failed reads halve this batch size.

        Raises:
            ValueError: The initial batch size is less than one.
        """
        if start < 1:
            msg = f"start must be >= 1, got {start}"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._table = table
        self._columns = columns
        self._start = start
        return self

    def __iter__(self) -> Iterator[list[dict[str, object]]]:
        offset, size = 0, self._start
        while True:
            batch = self._read_shrinking(offset, size)
            if not batch[0]:
                return
            yield batch[0]
            offset += len(batch[0])
            size = batch[1]

    def _read_shrinking(
        self, offset: int, size: int
    ) -> tuple[list[dict[str, object]], int]:
        while True:
            try:
                return self._read(offset, size), size
            except (OSError, RuntimeError, ValueError):
                if size == 1:
                    raise
                size //= 2
                logger.warning(
                    "scan read failed at row %d; batch size now %d", offset, size
                )

    def _read(self, offset: int, size: int) -> list[dict[str, object]]:
        query = self._table.search().select(self._columns).limit(size)
        return query.offset(offset).to_list()
