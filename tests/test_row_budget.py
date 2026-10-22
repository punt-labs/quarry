"""Tests for quarry.db.row_budget and its enforcement in ChunkTable.build_records."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from quarry.db.chunk_table import ChunkTable
from quarry.db.row_budget import RowBudget, RowBudgetError
from quarry.models import Chunk

_PAGE_LIMIT = 1_048_576
_CHUNK_LIMIT = 65_536


def _chunk(*, text: str = "chunk", page_raw_text: str = "page") -> Chunk:
    return Chunk(
        document_name="doc.txt",
        document_path="/docs/doc.txt",
        collection="col",
        page_number=1,
        total_pages=1,
        chunk_index=0,
        text=text,
        page_raw_text=page_raw_text,
        page_type="text",
        source_format=".txt",
        ingestion_timestamp=datetime.now(UTC),
    )


class TestInvariant:
    def test_defaults_sit_exactly_on_the_arrow_offset_limit(self) -> None:
        budget = RowBudget()
        assert (
            budget.max_page_bytes * budget.assumed_batch_rows * budget.safety_factor
            == 2**31
        )

    def test_a_larger_page_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(max_page_bytes=_PAGE_LIMIT + 1)

    def test_a_larger_chunk_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(max_chunk_bytes=_PAGE_LIMIT + 1)

    def test_a_larger_batch_assumption_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(assumed_batch_rows=2_048)

    def test_smaller_caps_are_accepted(self) -> None:
        RowBudget(max_page_bytes=1_024, max_chunk_bytes=512, assumed_batch_rows=1)


class TestRequirePage:
    def test_a_page_of_exactly_the_cap_is_accepted(self) -> None:
        RowBudget().require_page("a" * _PAGE_LIMIT)

    def test_a_page_one_byte_over_is_rejected(self) -> None:
        with pytest.raises(RowBudgetError, match="1,048,577 bytes > 1,048,576"):
            RowBudget().require_page("a" * (_PAGE_LIMIT + 1))

    def test_two_byte_characters_are_measured_in_bytes(self) -> None:
        budget = RowBudget()
        budget.require_page("é" * (_PAGE_LIMIT // 2))
        with pytest.raises(RowBudgetError):
            budget.require_page("é" * (_PAGE_LIMIT // 2 + 1))

    def test_four_byte_characters_are_measured_in_bytes(self) -> None:
        budget = RowBudget()
        budget.require_page("😀" * (_PAGE_LIMIT // 4))
        with pytest.raises(RowBudgetError):
            budget.require_page("😀" * (_PAGE_LIMIT // 4 + 1))

    def test_a_short_string_needs_no_measuring(self) -> None:
        RowBudget().require_page("")

    def test_the_message_names_the_column(self) -> None:
        with pytest.raises(RowBudgetError, match="page_raw_text"):
            RowBudget(max_page_bytes=8, assumed_batch_rows=1).require_page("a" * 9)

    def test_the_error_is_a_value_error(self) -> None:
        assert issubclass(RowBudgetError, ValueError)


class TestRequireChunk:
    def test_a_chunk_of_exactly_the_cap_is_accepted(self) -> None:
        RowBudget().require_chunk("a" * _CHUNK_LIMIT)

    def test_a_chunk_one_byte_over_is_rejected(self) -> None:
        with pytest.raises(RowBudgetError, match="text 65,537 bytes > 65,536"):
            RowBudget().require_chunk("a" * (_CHUNK_LIMIT + 1))

    def test_multibyte_chunks_are_measured_in_bytes(self) -> None:
        with pytest.raises(RowBudgetError):
            RowBudget().require_chunk("é" * (_CHUNK_LIMIT // 2 + 1))


class TestBuildRecordsBackstop:
    @staticmethod
    def _vectors(count: int = 1) -> np.ndarray:
        return np.zeros((count, 768), dtype=np.float32)

    def test_a_page_of_exactly_the_cap_is_built_untouched(self) -> None:
        page = "a" * _PAGE_LIMIT
        [record] = ChunkTable.build_records(
            [_chunk(page_raw_text=page)], self._vectors()
        )
        assert record["page_raw_text"] == page

    def test_a_page_one_byte_over_is_rejected_not_truncated(self) -> None:
        with pytest.raises(RowBudgetError, match="page_raw_text"):
            ChunkTable.build_records(
                [_chunk(page_raw_text="a" * (_PAGE_LIMIT + 1))], self._vectors()
            )

    def test_a_multibyte_page_is_measured_in_bytes(self) -> None:
        with pytest.raises(RowBudgetError):
            ChunkTable.build_records(
                [_chunk(page_raw_text="é" * (_PAGE_LIMIT // 2 + 1))], self._vectors()
            )

    def test_an_oversized_chunk_is_rejected(self) -> None:
        with pytest.raises(RowBudgetError, match="text"):
            ChunkTable.build_records(
                [_chunk(text="a" * (_CHUNK_LIMIT + 1))], self._vectors()
            )

    def test_one_bad_row_rejects_the_whole_batch(self) -> None:
        chunks = [_chunk(), _chunk(page_raw_text="a" * (_PAGE_LIMIT + 1))]
        with pytest.raises(RowBudgetError):
            ChunkTable.build_records(chunks, self._vectors(2))
