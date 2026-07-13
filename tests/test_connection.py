"""Unit tests for the self-recycling LanceDB connection proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, cast

import pytest

from quarry.db.connection import LanceConnection, RecyclingTable

if TYPE_CHECKING:
    from quarry.types import LanceDB


class FakeTable:
    """A minimal stand-in for a LanceDB table that records index rebuilds."""

    __slots__ = ("_fts", "_scalar")

    _fts: int
    _scalar: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._fts = 0
        self._scalar = 0
        return self

    @property
    def fts_rebuilds(self) -> int:
        """Number of full-text index rebuilds requested."""
        return self._fts

    @property
    def scalar_rebuilds(self) -> int:
        """Number of scalar index rebuilds requested."""
        return self._scalar

    def create_fts_index(self, column: str, *, replace: bool = False) -> None:
        """Record a full-text index rebuild."""
        self._fts += 1

    def create_scalar_index(
        self,
        column: str,
        *,
        index_type: str = "BTREE",
        replace: bool = False,
    ) -> None:
        """Record a scalar index rebuild."""
        self._scalar += 1


class FakeTables:
    """The ``.tables`` attribute of a fake ``list_tables`` result."""

    __slots__ = ("tables",)

    tables: list[str]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.tables = ["chunks"]
        return self


class FakeConnection:
    """A fake LanceDB connection; each instance is one physical connection."""

    __slots__ = ("_table",)

    _table: FakeTable

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._table = FakeTable()
        return self

    @property
    def table(self) -> FakeTable:
        """The single table this connection serves."""
        return self._table

    def open_table(self, name: str) -> FakeTable:
        """Return the connection's table."""
        return self._table

    def list_tables(self) -> FakeTables:
        """Return the fake table listing."""
        return FakeTables()


class ConnectionFactory:
    """Records how many physical connections were opened via ``get_db``."""

    __slots__ = ("_connections",)

    _connections: list[FakeConnection]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._connections = []
        return self

    @property
    def open_count(self) -> int:
        """How many connections have been created."""
        return len(self._connections)

    @property
    def latest(self) -> FakeConnection:
        """The most recently created connection."""
        return self._connections[-1]

    def __call__(self) -> LanceDB:
        conn = FakeConnection()
        self._connections.append(conn)
        return cast("LanceDB", conn)


@pytest.fixture()
def factory() -> ConnectionFactory:
    """Return a counting connection factory to inject into ``LanceConnection``."""
    return ConnectionFactory()


def test_open_table_returns_recycling_table(factory: ConnectionFactory) -> None:
    conn = LanceConnection(factory, recycle_after=3)
    assert isinstance(conn.open_table("chunks"), RecyclingTable)
    assert factory.open_count == 1


def test_fts_rebuild_delegates_to_wrapped_table(factory: ConnectionFactory) -> None:
    conn = LanceConnection(factory, recycle_after=99)
    conn.open_table("chunks").create_fts_index("text", replace=True)
    assert factory.latest.table.fts_rebuilds == 1


def test_recycles_after_threshold_rebuilds(factory: ConnectionFactory) -> None:
    """Crossing the rebuild cap reopens the connection at the next boundary."""
    conn = LanceConnection(factory, recycle_after=3)
    for _ in range(3):
        conn.open_table("chunks").create_fts_index("text", replace=True)
    assert factory.open_count == 1, "recycle must defer past the live table handle"
    conn.open_table("chunks")
    assert factory.open_count == 2, "next boundary must reopen the connection"


def test_does_not_recycle_below_threshold(factory: ConnectionFactory) -> None:
    conn = LanceConnection(factory, recycle_after=5)
    for _ in range(4):
        conn.open_table("chunks").create_scalar_index("collection", replace=True)
    conn.open_table("chunks")
    assert factory.open_count == 1


def test_recycle_counter_resets_after_recycle(factory: ConnectionFactory) -> None:
    """A second batch of rebuilds triggers a second recycle, not an early one."""
    conn = LanceConnection(factory, recycle_after=2)
    for _ in range(2):
        conn.open_table("chunks").create_fts_index("text", replace=True)
    conn.open_table("chunks")  # recycle #1
    assert factory.open_count == 2
    conn.open_table("chunks").create_fts_index("text", replace=True)
    conn.open_table("chunks")  # only 1 rebuild since recycle — no recycle yet
    assert factory.open_count == 2
    conn.open_table("chunks").create_fts_index("text", replace=True)
    conn.open_table("chunks")  # second rebuild crosses cap — recycle #2
    assert factory.open_count == 3
