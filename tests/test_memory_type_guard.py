"""Tests for quarry.ingestion.memory_type_guard: the storage memory_type vocabulary."""

from __future__ import annotations

import pytest

from quarry.ingestion.memory_type_guard import MemoryTypeGuard
from quarry.memory_types import MemoryType


def test_the_literal_is_the_enum_plus_unset() -> None:
    """Pin: a new MemoryType member must also join the pipeline's Literal."""
    assert set(MemoryTypeGuard.vocabulary()) == {*(m.value for m in MemoryType), ""}


@pytest.mark.parametrize(
    "raw", ["", "fact", "observation", "opinion", "procedure", "lesson"]
)
def test_every_storage_value_is_accepted(raw: str) -> None:
    guard = MemoryTypeGuard
    assert guard.check(raw)
    assert guard.require(raw) == raw


@pytest.mark.parametrize("raw", ["facts", "FACT", " fact", "other"])
def test_anything_else_is_refused(raw: str) -> None:
    guard = MemoryTypeGuard
    assert not guard.check(raw)
    with pytest.raises(ValueError, match="unknown memory_type"):
        guard.require(raw)


def test_the_vocabulary_cannot_be_replaced_per_instance() -> None:
    with pytest.raises(AttributeError):
        object.__setattr__(MemoryTypeGuard(), "allowed", ("anything",))
