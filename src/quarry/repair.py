"""Rebuild a chunks table that optimize() can no longer compact."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from lancedb.index import FTS, Bitmap

from quarry.atomic_dir_swap import AtomicDirectorySwap
from quarry.daemon_guard import DaemonGuard
from quarry.db.adaptive_scan import AdaptiveScan
from quarry.db.schema import TABLE_NAME
from quarry.db.storage import get_db
from quarry.repair_plan import RepairPlan
from quarry.repair_rows import RowJudge
from quarry.repair_scratch import RepairScratch
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from pathlib import Path

    import pyarrow as pa

    from quarry.types import LanceTable

logger = logging.getLogger(__name__)

# Batches stay far under the 2**31 byte Arrow offset limit; AdaptiveScan halves
# further if a region of the table still overflows.
BATCH_ROWS = 256
_VECTOR_BYTES = 4


@final
class TableRepair:
    """Rebuild the chunks table without its binary and over-budget rows.

    Never works in place: the kept rows go into a scratch database beside the
    run dir, are verified, and only then does :class:`AtomicDirectorySwap` rename
    the new table directory over the old one, reclaiming every leaked file with
    it.  A failure before the swap leaves the original table untouched.  Pages of
    a dropped document are left in the pages table: no chunk refers to them, so
    readers cannot see them and ``OrphanPageSweep`` deletes them after its grace
    period.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, db_path: Path) -> Self:
        self = super().__new__(cls)
        self._path = db_path
        return self

    def plan(self) -> RepairPlan:
        """Count what a rebuild would drop and keep; change nothing."""
        table = self._open()
        if table is None:
            return RepairPlan(0, 0, 0, 0, 0)
        judge = self._judge_for(table)
        rows = binary = over = live = 0
        for batch in AdaptiveScan(table, list(table.schema.names), start=BATCH_ROWS):
            for row in batch:
                rows += 1
                verdict = judge.verdict(row)
                binary += verdict == "binary"
                over += verdict == "over_budget"
                live += self._row_bytes(row) if verdict == "keep" else 0
        return RepairPlan(rows, binary, over, live, self._disk_bytes())

    def rebuild(self) -> int:
        """Rebuild the table and swap it in; return its new on-disk size in bytes.

        Raises ``ValueError`` when there is no chunks table, a kept row fails
        verification, or a daemon owns the run dir.  Any failure leaves the
        original table as it was.
        """
        run_dir = RunDir(self._path.parent)
        with DaemonGuard(run_dir).exclusive():
            table = self._open()
            if table is None:
                msg = f"no chunks table in {self._path}"
                raise ValueError(msg)
            judge = self._judge_for(table)
            with RepairScratch(self._path.parent).open() as scratch:
                self._swap_in(scratch, self._write_kept(table, judge, scratch))
        return self._disk_bytes()

    def _write_kept(self, table: LanceTable, judge: RowJudge, scratch: Path) -> int:
        schema = table.schema
        target = get_db(scratch).create_table(TABLE_NAME, data=[], schema=schema)
        kept = 0
        for batch in AdaptiveScan(table, list(schema.names), start=BATCH_ROWS):
            good = [row for row in batch if judge.verdict(row) == "keep"]
            for row in good:
                self._require_whole(row, schema)
            if good:
                target.add(good)
                kept += len(good)
        if target.count_rows() != kept:
            msg = f"rebuilt table holds {target.count_rows()} rows, expected {kept}"
            raise ValueError(msg)
        self._create_indexes(target)
        return kept

    def _swap_in(self, scratch: Path, kept: int) -> None:
        swap = AtomicDirectorySwap(TABLE_NAME, self._path / f"{TABLE_NAME}.lance")
        tmp = swap.prepare()
        (scratch / f"{TABLE_NAME}.lance").rename(tmp)
        swap.commit(tmp)
        logger.info("repaired %s: %d rows kept", self._path, kept)

    def _judge_for(self, table: LanceTable) -> RowJudge:
        judge = RowJudge(table.schema.names)
        for batch in AdaptiveScan(
            table, ["collection", "document_name", "text"], start=BATCH_ROWS
        ):
            for row in batch:
                judge.note(row)
        return judge

    def _require_whole(self, row: dict[str, object], schema: pa.Schema) -> None:
        for name in schema.names:
            if row.get(name) is None:
                msg = f"kept row is missing column {name!r}"
                raise ValueError(msg)
        if not self._vector_is_whole(row, schema):
            msg = f"kept row has an incomplete vector in {row['document_name']!r}"
            raise ValueError(msg)

    @staticmethod
    def _vector_is_whole(row: dict[str, object], schema: pa.Schema) -> bool:
        vector = row["vector"]
        width = schema.field("vector").type.list_size
        return isinstance(vector, list) and len(vector) == width

    @staticmethod
    def _create_indexes(table: LanceTable) -> None:
        table.create_index("collection", config=Bitmap(), replace=True)
        table.create_index("text", config=FTS(), replace=True)

    @staticmethod
    def _row_bytes(row: dict[str, object]) -> int:
        vector = row["vector"]
        width = len(vector) if isinstance(vector, list) else 0
        return len(str(row["text"]).encode("utf-8")) + width * _VECTOR_BYTES

    def _open(self) -> LanceTable | None:
        if not self._path.is_dir():
            return None
        db = get_db(self._path)
        if TABLE_NAME not in db.list_tables().tables:
            return None
        return db.open_table(TABLE_NAME)

    def _disk_bytes(self) -> int:
        total = 0
        for path in (self._path / f"{TABLE_NAME}.lance").rglob("*"):
            try:
                total += path.stat().st_size if path.is_file() else 0
            except FileNotFoundError:
                continue  # a file removed mid-walk is not part of the table
        return total
