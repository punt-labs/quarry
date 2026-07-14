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

    def create_table(
        self,
        name: str,
        *,
        data: list[dict[str, object]],
        schema: object,
    ) -> FakeTable:
        """Return the connection's table, ignoring the create arguments."""
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


class FlakyRecycleFactory:
    """Opens the first connection, fails the first reopen, then succeeds.

    Models a *transient* reopen failure — a momentary ``EMFILE`` exactly when
    descriptors are exhausted: the first recycle attempt raises, but the pending
    recycle must stay armed so a later boundary reopens cleanly onto a fresh
    connection. Call 1 is the initial open, call 2 is the failing reopen, call 3
    is the successful retry.
    """

    __slots__ = ("_calls", "_first", "_second")

    _calls: int
    _first: FakeConnection
    _second: FakeConnection

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = 0
        self._first = FakeConnection()
        self._second = FakeConnection()
        return self

    @property
    def calls(self) -> int:
        """How many times the factory was invoked, including the failed reopen."""
        return self._calls

    @property
    def first(self) -> FakeConnection:
        """The original physical connection, still live after a failed recycle."""
        return self._first

    @property
    def second(self) -> FakeConnection:
        """The replacement connection returned by the successful retry."""
        return self._second

    def __call__(self) -> LanceDB:
        self._calls += 1
        if self._calls == 1:
            return cast("LanceDB", self._first)
        if self._calls == 2:
            msg = "reopen failed: storage unavailable"
            raise OSError(msg)
        return cast("LanceDB", self._second)


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


def test_replace_false_does_not_advance_rebuild_counter(
    factory: ConnectionFactory,
) -> None:
    """Idempotent ``replace=False`` builds create nothing to leak, so they must
    not arm recycling — even well past the cap.
    """
    conn = LanceConnection(factory, recycle_after=1)
    for _ in range(5):
        conn.open_table("chunks").create_fts_index("text", replace=False)
        conn.open_table("chunks").create_scalar_index("collection", replace=False)
    conn.open_table("chunks")
    assert factory.open_count == 1, "replace=False must never trigger a recycle"


def test_replace_true_advances_rebuild_counter(factory: ConnectionFactory) -> None:
    """A single ``replace=True`` rebuild at the cap arms the next-boundary recycle."""
    conn = LanceConnection(factory, recycle_after=1)
    conn.open_table("chunks").create_fts_index("text", replace=True)
    conn.open_table("chunks")
    assert factory.open_count == 2, "replace=True must advance the recycle counter"


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


def test_create_table_fires_an_armed_recycle(factory: ConnectionFactory) -> None:
    """``create_table`` is a connection boundary too: an armed recycle must fire
    there, just as it does at ``open_table``/``list_tables``.
    """
    conn = LanceConnection(factory, recycle_after=1)
    conn.open_table("chunks").create_fts_index("text", replace=True)  # arm recycle
    assert factory.open_count == 1, "recycle defers past the live table handle"

    table = conn.create_table("archive", data=[], schema=object())
    assert factory.open_count == 2, "create_table must fire the armed recycle"
    assert isinstance(table, RecyclingTable)


def test_successful_recycle_leaves_no_armed_state(factory: ConnectionFactory) -> None:
    """After a clean recycle, both the recycle flag and the in-progress guard are
    cleared atomically: the very next boundary must not reopen again.
    """
    conn = LanceConnection(factory, recycle_after=1)
    conn.open_table("chunks").create_fts_index("text", replace=True)
    conn.open_table("chunks")  # crosses the cap — reopens once
    assert factory.open_count == 2, "the armed recycle fired exactly once"

    # No armed or in-progress state may linger: repeated boundaries are inert
    # until a fresh rebuild re-arms the counter.
    conn.open_table("chunks")
    conn.open_table("chunks")
    assert factory.open_count == 2, "a cleared recycle must not reopen again"


def test_failed_recycle_keeps_old_connection_and_rearms() -> None:
    """A reopen that raises stays armed: the old connection keeps serving until a
    later boundary retries the reopen and swaps in a fresh connection.
    """
    factory = FlakyRecycleFactory()
    conn = LanceConnection(factory, recycle_after=2)

    # Hold a live table handle, then arm the recycle with two rebuilds. Writes
    # through the handle bypass ``_maybe_recycle``, so they always hit whichever
    # connection currently backs the wrapper.
    table = conn.open_table("chunks")
    table.create_fts_index("text", replace=True)
    table.create_fts_index("text", replace=True)  # 2 rebuilds — recycle armed

    # (b) The reopen raises and propagates rather than being swallowed.
    with pytest.raises(OSError, match="reopen failed"):
        conn.open_table("chunks")  # recycle reopen (call 2) raises
    assert factory.calls == 2, "the reopen was attempted"

    # (a) The old connection is intact, not half-open: the still-live handle
    # keeps writing to it after the failed reopen.
    table.create_fts_index("text", replace=True)
    assert factory.first.table.fts_rebuilds == 3, "old connection still writes"

    # (c) The recycle stayed armed, so the next boundary retries the reopen. This
    # attempt (call 3) succeeds and swaps in the fresh connection.
    fresh = conn.open_table("chunks")
    assert factory.calls == 3, "recycle stayed armed and retried on next access"
    assert isinstance(fresh, RecyclingTable)
    fresh.create_fts_index("text", replace=True)
    assert factory.second.table.fts_rebuilds == 1, "fresh connection now serves"
