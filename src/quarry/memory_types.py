"""The closed ``memory_type`` vocabulary every write route validates against."""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self


class MemoryType(StrEnum):
    """The five values a chunk's ``memory_type`` column may carry.

    ``""`` (unset) is deliberately not a member: a plain document or capture
    carries no type, never decays, and is never boosted. ``LESSON`` is written
    only by ``learn``; the four others are the agent-owned, decayable classes
    an agent passes to ``remember``. Retrieval decay, the doctor's corpus
    tally, the route validators, and the CLI help text all read this one
    enum, so the vocabulary cannot drift between surfaces.
    """

    FACT = "fact"
    OBSERVATION = "observation"
    OPINION = "opinion"
    PROCEDURE = "procedure"
    LESSON = "lesson"

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Return the member for *raw*; raise ``ValueError`` naming the choices.

        The message is the exact text the daemon returns as a 400 body, so
        every surface reports an unknown type identically.
        """
        try:
            return cls(raw)
        except ValueError:
            msg = f"unknown memory_type {raw!r}; expected one of {cls.agent_choices()}"
            raise ValueError(msg) from None

    @classmethod
    def agent_choices(cls) -> str:
        """Return the agent-writable values as a sorted, comma-separated list."""
        return ", ".join(sorted(DECAYABLE_MEMORY_TYPES))

    @classmethod
    def write_rejection(cls, raw: str) -> str:
        """Return why an agent write may not carry *raw*, or ``""`` when it may.

        The one rule every write path (remember, ingest, capture) shares: an
        empty value is "unset" and passes; a value outside the vocabulary is
        rejected with the choices (a mistyped ``"facts"`` must not be stored
        as a row that neither decays nor matches a typed filter); and
        ``"lesson"`` is a valid type only ``learn`` may write, because
        retrieval's boost keys purely on that string.
        """
        if not raw:
            return ""
        try:
            parsed = cls.parse(raw)
        except ValueError as exc:
            return str(exc)
        if parsed is cls.LESSON:
            return f"memory_type '{parsed}' is reserved for quarry learn"
        return ""

    @property
    def is_decayable(self) -> bool:
        """Return whether rows of this type follow the recency curve.

        Every agent-owned type decays; a lesson is curated project knowledge
        and is boosted instead.
        """
        return self is not MemoryType.LESSON


DECAYABLE_MEMORY_TYPES: Final[frozenset[str]] = frozenset(
    member.value for member in MemoryType if member.is_decayable
)
