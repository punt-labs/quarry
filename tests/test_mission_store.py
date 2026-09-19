"""Behaviour of :class:`quarry.mission_store.MissionStore`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from quarry.mission_store import MissionScan, MissionStore
from tests.mission_fixtures import CLOSED_MISSION, OPEN_MISSION, repo_with_missions

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repo_with_missions(tmp_path / "repo")


def _store(repo: Path) -> MissionStore:
    store = MissionStore.for_repo(repo)
    assert store is not None
    return store


def _contract_less_directory(repo: Path) -> Path:
    """Leave the shape another checkout's dispatch leaves: a log, no contract."""
    stray = repo / ".punt-labs" / "ethos" / "missions" / "m-2026-09-30-003"
    stray.mkdir()
    (stray / "log-3f2a9c1e-0000-4000-8000-000000000000-1-1.jsonl").write_text(
        '{"event": "delegated"}\n'
    )
    return stray


class TestForRepo:
    def test_finds_the_tree_from_a_subdirectory(self, repo: Path) -> None:
        deep = repo / "src" / "pkg"
        deep.mkdir(parents=True)
        assert MissionStore.for_repo(deep) is not None

    def test_no_tree_is_none_with_an_info_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        (bare / ".git").mkdir()
        with caplog.at_level("INFO", logger="quarry.mission_store"):
            assert MissionStore.for_repo(bare) is None
        assert any(
            "no .punt-labs/ethos/missions/" in r.getMessage() for r in caplog.records
        )


class TestScan:
    def test_parses_both_fixture_missions_in_order(self, repo: Path) -> None:
        scan = _store(repo).scan()
        assert scan.errors == ()
        ids = [m.contract.mission_id for m in scan.missions]
        assert ids == sorted([CLOSED_MISSION, OPEN_MISSION])

    def test_closed_mission_freezes_its_reflectionless_final_round(
        self, repo: Path
    ) -> None:
        closed = next(
            m
            for m in _store(repo).scan().missions
            if m.contract.mission_id == CLOSED_MISSION
        )
        frozen = closed.frozen_rounds()
        assert [r.number for r in frozen] == [1, 2]
        assert frozen[1].has_result and not frozen[1].has_reflection

    def test_open_mission_freezes_only_completed_rounds(self, repo: Path) -> None:
        open_ = next(
            m
            for m in _store(repo).scan().missions
            if m.contract.mission_id == OPEN_MISSION
        )
        assert [r.number for r in open_.rounds] == [1, 2]
        assert [r.number for r in open_.frozen_rounds()] == [1]

    def test_repo_less_contract_takes_the_scanned_checkout(self, repo: Path) -> None:
        open_ = next(
            m
            for m in _store(repo).scan().missions
            if m.contract.mission_id == OPEN_MISSION
        )
        assert open_.contract.repo == str(repo)
        assert open_.contract.repo_name == "repo"

    def test_mission_filter_restricts_to_one_directory(self, repo: Path) -> None:
        scan = _store(repo).scan(mission_id=CLOSED_MISSION)
        assert [m.contract.mission_id for m in scan.missions] == [CLOSED_MISSION]

    def test_unknown_mission_filter_is_one_error(self, repo: Path) -> None:
        """Asking for a mission that is not there must not read as 'filed 0'."""
        scan = _store(repo).scan(mission_id="m-9999-99-99-999")
        assert scan.missions == ()
        missions_dir = repo / ".punt-labs" / "ethos" / "missions"
        assert scan.errors == (
            f"mission m-9999-99-99-999 not found under {missions_dir}",
        )

    def test_contract_less_directory_is_skipped(self, repo: Path) -> None:
        """A delegation log with no contract.yaml is not a mission: no error line."""
        _contract_less_directory(repo)
        scan = _store(repo).scan()
        assert scan.errors == ()
        ids = [m.contract.mission_id for m in scan.missions]
        assert ids == sorted([CLOSED_MISSION, OPEN_MISSION])

    def test_contract_less_mission_filter_is_an_empty_scan(self, repo: Path) -> None:
        """A named directory that holds no contract is a skip, not an error."""
        stray = _contract_less_directory(repo)
        scan = _store(repo).scan(mission_id=stray.name)
        assert scan == MissionScan(missions=(), errors=())

    def test_absent_round_files_read_as_no_rounds(self, repo: Path) -> None:
        missions_dir = repo / ".punt-labs" / "ethos" / "missions"
        (missions_dir / CLOSED_MISSION / "results.yaml").unlink()
        (missions_dir / CLOSED_MISSION / "reflections.yaml").unlink()
        closed = next(
            m
            for m in _store(repo).scan().missions
            if m.contract.mission_id == CLOSED_MISSION
        )
        assert closed.rounds == ()


class TestScanErrors:
    """One bad directory is one error line; the rest of the scan proceeds."""

    def _missions_dir(self, repo: Path) -> Path:
        return repo / ".punt-labs" / "ethos" / "missions"

    def test_malformed_yaml_is_collected(self, repo: Path) -> None:
        bad = self._missions_dir(repo) / "m-2026-09-30-001"
        bad.mkdir()
        (bad / "contract.yaml").write_text("mission_id: [unclosed\n")
        scan = _store(repo).scan()
        assert len(scan.missions) == 2
        assert len(scan.errors) == 1
        assert "m-2026-09-30-001" in scan.errors[0]

    def test_missing_worker_is_collected(self, repo: Path) -> None:
        bad = self._missions_dir(repo) / "m-2026-09-30-002"
        bad.mkdir()
        (bad / "contract.yaml").write_text(
            "mission_id: m-2026-09-30-002\ncurrent_round: 1\n"
        )
        scan = _store(repo).scan()
        assert any("worker" in e for e in scan.errors)

    def test_non_integer_round_is_collected(self, repo: Path) -> None:
        results = self._missions_dir(repo) / CLOSED_MISSION / "results.yaml"
        results.write_text("results:\n  - round: two\n")
        scan = _store(repo).scan()
        assert any(
            CLOSED_MISSION in e and "round must be an integer" in e for e in scan.errors
        )
        assert [m.contract.mission_id for m in scan.missions] == [OPEN_MISSION]

    def test_non_mapping_contract_is_collected(self, repo: Path) -> None:
        (self._missions_dir(repo) / CLOSED_MISSION / "contract.yaml").write_text(
            "- a\n"
        )
        scan = _store(repo).scan()
        assert any("not a mapping" in e for e in scan.errors)

    def test_non_list_results_is_collected(self, repo: Path) -> None:
        (self._missions_dir(repo) / CLOSED_MISSION / "results.yaml").write_text(
            "results: nope\n"
        )
        scan = _store(repo).scan()
        assert any("'results' is not a list" in e for e in scan.errors)

    def test_stray_files_in_the_tree_are_ignored(self, repo: Path) -> None:
        (self._missions_dir(repo) / "missions.jsonl").write_text("{}\n")
        assert _store(repo).scan().errors == ()
