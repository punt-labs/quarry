"""Tests for quarry.ingestion.plain_flush_target: single-document flush target."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

from quarry.ingestion.plain_flush_target import PlainFlushTarget

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk


def test_records_are_built_by_the_chunk_table() -> None:
    chunks = cast("list[Chunk]", [MagicMock()])
    vectors = cast("NDArray[np.float32]", MagicMock())
    with patch(
        "quarry.ingestion.plain_flush_target.ChunkTable.build_records",
        return_value=[{"x": 1}],
    ) as build:
        assert PlainFlushTarget(MagicMock()).build_records(chunks, vectors) == [
            {"x": 1}
        ]
    build.assert_called_once_with(chunks, vectors)


def test_inserting_writes_through_the_store() -> None:
    store = MagicMock()
    store.insert_records.return_value = 2
    assert PlainFlushTarget(store).insert_records([{}, {}]) == 2
    store.insert_records.assert_called_once_with([{}, {}])


def test_a_flush_commits_nothing() -> None:
    store = MagicMock()
    PlainFlushTarget(store).on_flush([])
    assert store.mock_calls == []
