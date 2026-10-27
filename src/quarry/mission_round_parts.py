"""The two halves of a mission round as ethos writes them to disk."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping


@final
class RoundEntry:
    """Field readers for one per-round YAML entry, shared by both halves.

    A namespace class rather than free functions (PY-OO-7): the two records
    below parse the same untrusted mapping vocabulary, so the readers have
    one home. Every method is stateless — ``__slots__ = ()``, all static.
    """

    __slots__ = ()

    @staticmethod
    def text(mapping: Mapping[str, object], key: str) -> str:
        """Return *key* as text; a missing value reads as ``""``."""
        value = mapping.get(key)
        if isinstance(value, str):
            return value
        return "" if value is None else str(value)

    @staticmethod
    def round_number(mapping: Mapping[str, object]) -> int:
        """Return the ``round`` field, raising on anything but an int."""
        value = mapping.get("round")
        if isinstance(value, bool) or not isinstance(value, int):
            msg = f"round must be an integer, got {value!r}"
            raise ValueError(msg)
        return value

    @staticmethod
    def strings(mapping: Mapping[str, object], key: str) -> tuple[str, ...]:
        """Return *key* as a tuple of strings; anything else reads as empty."""
        value = mapping.get(key)
        if not isinstance(value, list):
            return ()
        return tuple(str(item) for item in value)

    @staticmethod
    def number(mapping: Mapping[str, object], key: str) -> float:
        """Return *key* as a float; a missing or non-numeric value reads as 0.0."""
        value = mapping.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)


@final
@dataclass(frozen=True, slots=True)
class WorkerResult:
    """One ``results.yaml`` entry: the worker's own submission for a round.

    ``verdict`` is the worker's self-assessment, not the evaluator's judgement;
    the composer labels it that way.
    """

    round: int
    author: str
    verdict: str
    confidence: float
    open_questions: tuple[str, ...]
    prose: str

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> Self:
        """Parse one result entry; raises ``ValueError`` on a bad ``round``."""
        return cls(
            round=RoundEntry.round_number(mapping),
            author=RoundEntry.text(mapping, "author"),
            verdict=RoundEntry.text(mapping, "verdict"),
            confidence=RoundEntry.number(mapping, "confidence"),
            open_questions=RoundEntry.strings(mapping, "open_questions"),
            prose=RoundEntry.text(mapping, "prose").rstrip(),
        )


@final
@dataclass(frozen=True, slots=True)
class EvaluatorReflection:
    """One ``reflections.yaml`` entry: the round's distilled review signals."""

    round: int
    author: str
    converging: bool
    signals: tuple[str, ...]
    recommendation: str
    reason: str

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> Self:
        """Parse one reflection entry; raises ``ValueError`` on a bad ``round``."""
        return cls(
            round=RoundEntry.round_number(mapping),
            author=RoundEntry.text(mapping, "author"),
            converging=mapping.get("converging") is True,
            signals=RoundEntry.strings(mapping, "signals"),
            recommendation=RoundEntry.text(mapping, "recommendation"),
            reason=RoundEntry.text(mapping, "reason").strip(),
        )
