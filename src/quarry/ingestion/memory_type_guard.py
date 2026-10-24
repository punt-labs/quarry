"""Narrow a plain string to the pipeline's ``memory_type`` storage vocabulary."""

from __future__ import annotations

from typing import ClassVar, TypeIs, final, get_args

from quarry.ingestion.ingest_context import MemoryType


@final
class MemoryTypeGuard:
    """The one place a ``str`` becomes an :data:`ingest_context.MemoryType`.

    The daemon boundary carries plain strings; the pipeline's
    :class:`IngestContext` carries the closed Literal.  The vocabulary is read
    from the Literal itself and cannot be replaced by a caller, so the
    ``TypeIs`` narrowing never lies; a test pins it to the
    ``quarry.memory_types.MemoryType`` enum plus ``""`` (unset).
    """

    __slots__ = ()

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(get_args(MemoryType.__value__))

    @classmethod
    def vocabulary(cls) -> frozenset[str]:
        """Return every ``memory_type`` the pipeline stores, ``""`` included."""
        return cls._ALLOWED

    @classmethod
    def check(cls, raw: str) -> TypeIs[MemoryType]:
        """Return whether *raw* is a storage ``memory_type``."""
        return raw in cls._ALLOWED

    @classmethod
    def require(cls, raw: str) -> MemoryType:
        """Return *raw* narrowed to ``MemoryType``, or raise ``ValueError``."""
        if not cls.check(raw):
            msg = f"unknown memory_type {raw!r}"
            raise ValueError(msg)
        return raw
