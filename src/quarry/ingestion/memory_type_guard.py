"""Narrow a plain string to the pipeline's ``memory_type`` storage vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeIs, final, get_args

from quarry.ingestion.ingest_context import MemoryType


@final
@dataclass(frozen=True, slots=True)
class MemoryTypeGuard:
    """The one place a ``str`` becomes an :data:`ingest_context.MemoryType`.

    The daemon boundary carries plain strings; the pipeline's
    :class:`IngestContext` carries the closed Literal.
    The vocabulary is read from the Literal itself, and a test pins it to the
    ``quarry.memory_types.MemoryType`` enum plus ``""`` (unset).
    """

    allowed: tuple[str, ...] = get_args(MemoryType.__value__)

    def check(self, raw: str) -> TypeIs[MemoryType]:
        """Return whether *raw* is a storage ``memory_type``."""
        return raw in self.allowed

    def require(self, raw: str) -> MemoryType:
        """Return *raw* narrowed to ``MemoryType``, or raise ``ValueError``."""
        if not self.check(raw):
            msg = f"unknown memory_type {raw!r}"
            raise ValueError(msg)
        return raw
