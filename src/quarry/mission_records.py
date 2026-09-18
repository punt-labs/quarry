"""A mission contract and its rounds, read from the ethos sidecar files.

Quarry reads these files; it never calls ethos. The shapes are the ones
``ethos mission`` writes: ``contract.yaml`` plus the append-only per-round
lists in ``results.yaml`` and ``reflections.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quarry.mission_round_parts import EvaluatorReflection, WorkerResult

_OPEN = "open"


@final
@dataclass(frozen=True, slots=True)
class MissionContract:
    """The fields of ``contract.yaml`` a memory needs.

    ``repo`` is always present on the record: older contracts have no ``repo:``
    key, so the store supplies the checkout it is scanning as the default.
    """

    mission_id: str
    status: str
    created_at: str
    repo: str
    leader: str
    worker: str
    evaluator: str
    current_round: int

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object], *, default_repo: Path) -> Self:
        """Parse a contract; raises ``ValueError`` naming the first bad field."""
        return cls(
            mission_id=cls._required(mapping, "mission_id"),
            status=str(mapping.get("status", _OPEN)),
            created_at=str(mapping.get("created_at", "")),
            repo=str(mapping.get("repo") or default_repo),
            leader=str(mapping.get("leader", "")),
            worker=cls._required(mapping, "worker"),
            evaluator=cls._evaluator(mapping.get("evaluator")),
            current_round=cls._current_round(mapping.get("current_round")),
        )

    @property
    def repo_name(self) -> str:
        """Return the repo's basename — the discriminator in a memory's name."""
        return Path(self.repo).name or "root"

    @property
    def is_open(self) -> bool:
        return self.status == _OPEN

    @staticmethod
    def _required(mapping: Mapping[str, object], key: str) -> str:
        value = mapping.get(key)
        if not isinstance(value, str) or not value.strip():
            msg = f"contract field {key!r} is missing or blank"
            raise ValueError(msg)
        return value

    @staticmethod
    def _evaluator(value: object) -> str:
        """Return the evaluator handle from the pinned mapping or a bare string."""
        if isinstance(value, dict):
            handle = value.get("handle")
            return handle if isinstance(handle, str) else ""
        return value if isinstance(value, str) else ""

    @staticmethod
    def _current_round(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            msg = f"contract field 'current_round' must be an integer, got {value!r}"
            raise ValueError(msg)
        return value


@final
@dataclass(frozen=True, slots=True)
class MissionRound:
    """One numbered round: the worker's result and the evaluator's reflection.

    Either half may be absent — a closed mission's final round usually has a
    result and no reflection, and a failed close can freeze a round with
    either half missing. ``None`` is that documented absence, and the truth
    table in :meth:`is_empty` decides file-or-skip from the two halves alone.
    """

    number: int
    result: WorkerResult | None  # None: the worker never submitted this round
    reflection: EvaluatorReflection | None  # None: the evaluator never reflected

    @property
    def has_result(self) -> bool:
        return self.result is not None

    @property
    def has_reflection(self) -> bool:
        return self.reflection is not None

    @property
    def is_empty(self) -> bool:
        """Return whether the round has nothing to remember (neither half)."""
        return self.result is None and self.reflection is None

    def is_frozen(self, contract: MissionContract) -> bool:
        """Return whether ethos can no longer append to this round.

        ``advance`` requires a reflection, so a round below ``current_round``
        is complete; once the mission is not ``open`` (closed or failed),
        every round is. That immutability is what makes syncing idempotent
        without a ledger.
        """
        return not contract.is_open or self.number < contract.current_round
