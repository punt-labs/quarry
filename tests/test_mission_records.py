"""Behaviour of :class:`MissionContract` and :class:`MissionRound`."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.mission_records import MissionContract, MissionRound
from tests.mission_fixtures import Rounds

_ROOT = Path("/home/someone/Coding/punt-labs/quarry")


def _contract(**fields: object) -> MissionContract:
    mapping: dict[str, object] = {
        "mission_id": "m-2026-09-09-001",
        "status": "closed",
        "created_at": "2026-09-09T05:14:15Z",
        "worker": "gvr",
        "evaluator": {"handle": "rmh", "hash": "0539"},
        "current_round": 2,
    }
    mapping.update(fields)
    return MissionContract.from_mapping(mapping, default_repo=_ROOT)


class TestMissionContract:
    def test_parses_the_real_shape(self) -> None:
        contract = _contract(repo="/elsewhere/quarry", leader="claude")
        assert contract.mission_id == "m-2026-09-09-001"
        assert contract.worker == "gvr"
        assert contract.evaluator == "rmh"
        assert contract.repo == "/elsewhere/quarry"
        assert contract.repo_name == "quarry"
        assert contract.current_round == 2
        assert contract.is_open is False

    def test_missing_repo_falls_back_to_the_scanned_checkout(self) -> None:
        """Older contracts have no ``repo:``; the store's root stands in."""
        assert _contract().repo == str(_ROOT)
        assert _contract().repo_name == "quarry"

    def test_bare_string_evaluator_is_accepted(self) -> None:
        assert _contract(evaluator="djb").evaluator == "djb"

    def test_unusable_evaluator_reads_as_empty(self) -> None:
        assert _contract(evaluator=42).evaluator == ""
        assert _contract(evaluator={"pinned_at": "x"}).evaluator == ""

    @pytest.mark.parametrize("field", ["mission_id", "worker"])
    def test_required_field_missing_or_blank_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            _contract(**{field: "  "})

    @pytest.mark.parametrize("bad", ["2", None, True])
    def test_non_integer_current_round_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="current_round"):
            _contract(current_round=bad)

    def test_status_defaults_to_open(self) -> None:
        mapping: dict[str, object] = {
            "mission_id": "m-1",
            "worker": "rmh",
            "current_round": 1,
        }
        assert MissionContract.from_mapping(mapping, default_repo=_ROOT).is_open


class TestMissionRoundFrozen:
    """Frozen iff the round is below current_round or the mission is not open."""

    def test_open_mission_round_below_current_is_frozen(self) -> None:
        contract = Rounds.contract(status="open", current_round=2)
        assert Rounds.full(1).is_frozen(contract) is True

    def test_open_mission_current_round_is_not_frozen(self) -> None:
        contract = Rounds.contract(status="open", current_round=2)
        assert Rounds.full(2).is_frozen(contract) is False

    @pytest.mark.parametrize("status", ["closed", "failed"])
    def test_every_round_of_a_non_open_mission_is_frozen(self, status: str) -> None:
        contract = Rounds.contract(status=status, current_round=2)
        assert Rounds.full(2).is_frozen(contract) is True
        assert Rounds.full(3).is_frozen(contract) is True


class TestMissionRoundHalves:
    def test_both_halves(self) -> None:
        round_ = Rounds.full()
        assert round_.has_result and round_.has_reflection
        assert round_.is_empty is False

    def test_result_only(self) -> None:
        round_ = MissionRound(2, Rounds.result(2), None)
        assert round_.has_result is True
        assert round_.has_reflection is False
        assert round_.is_empty is False

    def test_reflection_only(self) -> None:
        round_ = MissionRound(1, None, Rounds.reflection(1))
        assert round_.has_result is False
        assert round_.is_empty is False

    def test_neither_is_empty(self) -> None:
        assert MissionRound(1, None, None).is_empty is True
