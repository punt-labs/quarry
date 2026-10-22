"""Tests for quarry.db.row_budget and its enforcement in ChunkTable.build_records."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from quarry.db.chunk_table import ChunkTable
from quarry.db.row_budget import RowBudget, RowBudgetError
from quarry.models import Chunk

_PAGE_LIMIT = 1_048_575
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
    def test_defaults_sit_just_under_the_arrow_offset_limit(self) -> None:
        budget = RowBudget()
        worst = budget.max_page_bytes * budget.assumed_batch_rows * budget.safety_factor
        assert worst == 2**31 - 1024 * 2

    @pytest.mark.parametrize(
        "field", ["max_page_bytes", "assumed_batch_rows", "safety_factor"]
    )
    def test_every_field_must_be_positive(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            RowBudget(**{field: 0})

    def test_a_chunk_cap_must_hold_one_character(self) -> None:
        with pytest.raises(ValueError, match="max_chunk_bytes"):
            RowBudget(max_chunk_bytes=3)

    def test_the_chunk_character_bound_has_one_source(self) -> None:
        assert RowBudget().max_chunk_chars == 16_384
        assert RowBudget(max_chunk_bytes=4097).max_chunk_chars == 1024

    def test_a_larger_chunk_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(max_chunk_bytes=2**20)

    def test_a_larger_batch_assumption_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(assumed_batch_rows=2_048)

    def test_smaller_caps_are_accepted(self) -> None:
        RowBudget(max_page_bytes=1_024, max_chunk_bytes=512, assumed_batch_rows=1)


class TestRequirePage:
    def test_a_page_of_exactly_the_cap_is_accepted(self) -> None:
        RowBudget().require_page("a" * _PAGE_LIMIT)

    def test_a_page_one_byte_over_is_rejected(self) -> None:
        with pytest.raises(RowBudgetError, match="1,048,576 bytes > 1,048,575"):
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


_LABEL_LIMIT = 8_192


class TestLabelBudget:
    """summary, names and paths are copied onto every chunk row, so they are capped."""

    @pytest.mark.parametrize("column", ["summary", "document_name", "document_path"])
    def test_a_label_of_exactly_the_cap_is_accepted(self, column: str) -> None:
        RowBudget().require_label(column, "a" * _LABEL_LIMIT)

    @pytest.mark.parametrize("column", ["summary", "document_name", "document_path"])
    def test_a_label_one_byte_over_is_rejected_naming_the_column(
        self, column: str
    ) -> None:
        with pytest.raises(RowBudgetError) as info:
            RowBudget().require_label(column, "a" * (_LABEL_LIMIT + 1))
        assert str(info.value) == (f"row budget exceeded: {column} 8,193 bytes > 8,192")

    def test_label_bytes_are_multibyte_aware(self) -> None:
        RowBudget().require_label("summary", "é" * (_LABEL_LIMIT // 2))
        with pytest.raises(RowBudgetError):
            RowBudget().require_label("summary", "é" * (_LABEL_LIMIT // 2 + 1))

    def test_the_label_cap_is_part_of_the_batch_arithmetic(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(max_label_bytes=2**20)

    def test_the_label_cap_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_label_bytes"):
            RowBudget(max_label_bytes=0)

    def test_a_three_mib_summary_on_many_chunks_is_refused(self) -> None:
        """The unbounded column that could still overflow a 1,024-row batch."""
        with pytest.raises(RowBudgetError, match="summary"):
            RowBudget().require_label("summary", "s" * 3 * 2**20)


class TestStrictBound:
    def test_the_largest_budget_under_the_arrow_limit_is_accepted(self) -> None:
        RowBudget(
            max_page_bytes=2**31 - 1,
            max_chunk_bytes=4,
            max_label_bytes=4,
            assumed_batch_rows=1,
            safety_factor=1,
        )

    def test_one_byte_more_is_refused(self) -> None:
        with pytest.raises(ValueError, match="2\\*\\*31"):
            RowBudget(
                max_page_bytes=2**31,
                max_chunk_bytes=4,
                max_label_bytes=4,
                assumed_batch_rows=1,
                safety_factor=1,
            )


class TestBuildRecordsLabels:
    @pytest.mark.parametrize(
        ("column", "chunk"),
        [
            ("summary", replace(_chunk(), summary="x" * (_LABEL_LIMIT + 1))),
            (
                "document_name",
                replace(_chunk(), document_name="x" * (_LABEL_LIMIT + 1)),
            ),
            (
                "document_path",
                replace(_chunk(), document_path="x" * (_LABEL_LIMIT + 1)),
            ),
            ("collection", replace(_chunk(), collection="x" * (_LABEL_LIMIT + 1))),
            ("agent_handle", replace(_chunk(), agent_handle="x" * (_LABEL_LIMIT + 1))),
        ],
    )
    def test_an_oversized_label_is_rejected_not_truncated(
        self, column: str, chunk: Chunk
    ) -> None:
        with pytest.raises(RowBudgetError) as info:
            ChunkTable.build_records([chunk], np.zeros((1, 768), dtype=np.float32))
        assert str(info.value) == (
            f"row budget exceeded: {column} {_LABEL_LIMIT + 1:,} bytes > "
            f"{_LABEL_LIMIT:,}"
        )

    def test_labels_at_the_cap_are_built_untouched(self) -> None:
        chunk = replace(_chunk(), summary="x" * _LABEL_LIMIT)
        [record] = ChunkTable.build_records(
            [chunk], np.zeros((1, 768), dtype=np.float32)
        )
        assert record["summary"] == "x" * _LABEL_LIMIT
