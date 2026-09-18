"""Shared helpers for the Loop 2 (mission memory) tests.

The fixture YAML trios under ``tests/fixtures/missions/`` mirror two real
ethos shapes: a closed mission whose final round has a result and no
reflection, and an older contract with no ``repo:`` key whose current round
is still open. Tests copy them into a throwaway repo-shaped tree so the
ancestor walk never reaches this checkout's own runtime state.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.mission_records import MissionContract, MissionRound
from quarry.mission_round_parts import EvaluatorReflection, WorkerResult

if TYPE_CHECKING:
    from collections.abc import Iterable

FIXTURE_MISSIONS = Path(__file__).resolve().parent / "fixtures" / "missions"

CLOSED_MISSION = "m-2026-09-09-001"
OPEN_MISSION = "m-2026-09-02-003"


def repo_with_missions(
    root: Path, mission_ids: Iterable[str] = (CLOSED_MISSION, OPEN_MISSION)
) -> Path:
    """Copy the fixture missions into ``root/.punt-labs/ethos/missions/``."""
    missions_dir = root / ".punt-labs" / "ethos" / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)
    for mission_id in mission_ids:
        shutil.copytree(FIXTURE_MISSIONS / mission_id, missions_dir / mission_id)
    return root


@final
class Rounds:
    """Hand-built records for composer tests that need one exact shape."""

    __slots__ = ()

    @staticmethod
    def contract(**overrides: str | int) -> MissionContract:
        base: dict[str, str | int] = {
            "mission_id": "m-2026-09-02-003",
            "status": "open",
            "created_at": "2026-09-02T12:42:20Z",
            "repo": "/home/someone/quarry",
            "leader": "claude",
            "worker": "rmh",
            "evaluator": "djb",
            "current_round": 2,
        }
        base.update(overrides)
        return MissionContract(
            mission_id=str(base["mission_id"]),
            status=str(base["status"]),
            created_at=str(base["created_at"]),
            repo=str(base["repo"]),
            leader=str(base["leader"]),
            worker=str(base["worker"]),
            evaluator=str(base["evaluator"]),
            current_round=int(base["current_round"]),
        )

    @staticmethod
    def result(number: int = 1, **overrides: object) -> WorkerResult:
        return WorkerResult.from_mapping(
            {
                "round": number,
                "author": "rmh",
                "verdict": "pass",
                "confidence": 0.9,
                "open_questions": ["Two coupling-ratchet metrics were relaxed."],
                "prose": "Round 1: IgnoreRules extracted.\n",
                **overrides,
            }
        )

    @staticmethod
    def reflection(number: int = 1, **overrides: object) -> EvaluatorReflection:
        return EvaluatorReflection.from_mapping(
            {
                "round": number,
                "author": "claude",
                "converging": True,
                "signals": [
                    "evaluator djb: REJECT — blocker: WatchedTree._watches mutated "
                    "from event-loop and observer threads with no lock; release() "
                    "can abort mid-cleanup and re-leak watches",
                    "evaluator djb: major — schedule_tree aborts the whole tree",
                ],
                "recommendation": "continue",
                "reason": "Round 1's DRY consolidation stands.",
                **overrides,
            }
        )

    @classmethod
    def full(cls, number: int = 1) -> MissionRound:
        return MissionRound(number, cls.result(number), cls.reflection(number))
