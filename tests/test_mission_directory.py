"""Behaviour of :class:`quarry.mission_directory.MissionDirectory`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from quarry.mission_directory import MissionDirectory
from quarry.path_guard import SealedTree, SealedTreeError
from tests.mission_fixtures import CLOSED_MISSION, OPEN_MISSION, repo_with_missions

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repo_with_missions(tmp_path / "repo")


def _missions_dir(repo: Path) -> Path:
    return repo / ".punt-labs" / "ethos" / "missions"


def _closed(repo: Path) -> MissionDirectory:
    return MissionDirectory(_missions_dir(repo) / CLOSED_MISSION)


class TestHasContract:
    def test_true_for_a_fixture_mission(self, repo: Path) -> None:
        assert _closed(repo).has_contract()

    def test_false_for_a_log_only_directory(self, repo: Path) -> None:
        stray = _missions_dir(repo) / "m-2026-09-30-003"
        stray.mkdir()
        (stray / "log-3f2a9c1e-0000-4000-8000-000000000000-1-1.jsonl").write_text(
            '{"event": "delegated"}\n'
        )
        assert not MissionDirectory(stray).has_contract()

    def test_false_when_the_contract_is_a_directory(self, repo: Path) -> None:
        stray = _missions_dir(repo) / "m-2026-09-30-004"
        (stray / "contract.yaml").mkdir(parents=True)
        assert not MissionDirectory(stray).has_contract()

    def test_path_is_the_directory_given(self, repo: Path) -> None:
        assert _closed(repo).path == _missions_dir(repo) / CLOSED_MISSION


class TestContract:
    def test_parses_the_fixture_contract(self, repo: Path) -> None:
        assert _closed(repo).contract(repo).mission_id == CLOSED_MISSION

    def test_default_repo_fills_a_contract_with_no_repo_key(self, repo: Path) -> None:
        open_ = MissionDirectory(_missions_dir(repo) / OPEN_MISSION)
        assert open_.contract(repo).repo == str(repo)

    def test_non_mapping_raises(self, repo: Path) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "contract.yaml").write_text("- a\n")
        with pytest.raises(ValueError, match="not a mapping"):
            _closed(repo).contract(repo)

    def test_malformed_yaml_raises(self, repo: Path) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "contract.yaml").write_text(
            "mission_id: [unclosed\n"
        )
        with pytest.raises(yaml.YAMLError):
            _closed(repo).contract(repo)

    def test_absent_contract_raises_rather_than_returning_nothing(
        self, repo: Path
    ) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "contract.yaml").unlink()
        with pytest.raises(FileNotFoundError):
            _closed(repo).contract(repo)


class TestRounds:
    def test_pairs_results_with_reflections_in_round_order(self, repo: Path) -> None:
        rounds = _closed(repo).rounds()
        assert [r.number for r in rounds] == [1, 2]
        assert rounds[1].has_result and not rounds[1].has_reflection

    def test_absent_round_files_are_no_rounds(self, repo: Path) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "results.yaml").unlink()
        (_missions_dir(repo) / CLOSED_MISSION / "reflections.yaml").unlink()
        assert _closed(repo).rounds() == ()

    def test_an_empty_key_is_no_rounds(self, repo: Path) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "results.yaml").write_text("results:\n")
        (_missions_dir(repo) / CLOSED_MISSION / "reflections.yaml").unlink()
        assert _closed(repo).rounds() == ()

    def test_non_mapping_entries_are_dropped(self, repo: Path) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "results.yaml").write_text(
            "results:\n  - 3\n  - not-a-mapping\n"
        )
        (_missions_dir(repo) / CLOSED_MISSION / "reflections.yaml").unlink()
        assert _closed(repo).rounds() == ()

    def test_non_list_raises(self, repo: Path) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "results.yaml").write_text(
            "results: nope\n"
        )
        with pytest.raises(ValueError, match="'results' is not a list"):
            _closed(repo).rounds()


class TestGuard:
    """Every file read goes through the directory's path guard."""

    def test_default_guard_follows_a_symlinked_file(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Without a trust boundary named, a link is just a file (the old shape)."""
        outside = tmp_path / "outside.yaml"
        outside.write_text("results:\n")
        target = _missions_dir(repo) / CLOSED_MISSION / "results.yaml"
        target.unlink()
        target.symlink_to(outside)
        (_missions_dir(repo) / CLOSED_MISSION / "reflections.yaml").unlink()
        assert _closed(repo).rounds() == ()

    def test_sealed_guard_refuses_a_symlinked_file(
        self, repo: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.yaml"
        outside.write_text("results:\n")
        target = _missions_dir(repo) / CLOSED_MISSION / "results.yaml"
        target.unlink()
        target.symlink_to(outside)
        sealed = MissionDirectory(
            _missions_dir(repo) / CLOSED_MISSION, guard=SealedTree(repo)
        )
        with pytest.raises(SealedTreeError, match="symlink"):
            sealed.rounds()

    def test_sealed_guard_refuses_a_symlinked_contract(
        self, repo: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "contract.yaml"
        outside.write_text("mission_id: x\nworker: w\n")
        target = _missions_dir(repo) / CLOSED_MISSION / "contract.yaml"
        target.unlink()
        target.symlink_to(outside)
        sealed = MissionDirectory(
            _missions_dir(repo) / CLOSED_MISSION, guard=SealedTree(repo)
        )
        assert sealed.has_contract()  # present — so the failure is loud, not a skip
        with pytest.raises(SealedTreeError, match="symlink"):
            sealed.contract(repo)

    def test_sealed_guard_reads_a_real_file(self, repo: Path) -> None:
        sealed = MissionDirectory(
            _missions_dir(repo) / CLOSED_MISSION, guard=SealedTree(repo)
        )
        assert sealed.contract(repo).mission_id == CLOSED_MISSION

    def test_sealed_guard_refuses_a_symlinked_mission_directory(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A real contract below a linked directory is still another tree's."""
        other = repo_with_missions(tmp_path / "other", (CLOSED_MISSION,))
        link = _missions_dir(repo) / "m-2026-09-30-009"
        link.symlink_to(other / ".punt-labs" / "ethos" / "missions" / CLOSED_MISSION)
        sealed = MissionDirectory(link, guard=SealedTree(repo))
        assert sealed.has_contract()  # present — the read refuses it loudly
        with pytest.raises(SealedTreeError, match="symlink"):
            sealed.contract(repo)
        with pytest.raises(SealedTreeError, match="symlink"):
            sealed.rounds()

    def test_sealed_guard_refuses_a_directory_swapped_for_a_link_after_listing(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A listing that saw a real directory grants nothing to the later read."""
        other = repo_with_missions(tmp_path / "other", (CLOSED_MISSION,))
        mission = _missions_dir(repo) / CLOSED_MISSION
        seal = SealedTree(repo)
        assert seal.check(mission) == mission
        sealed = MissionDirectory(mission, guard=seal)
        mission.rename(tmp_path / "moved")
        mission.symlink_to(other / ".punt-labs" / "ethos" / "missions" / CLOSED_MISSION)
        with pytest.raises(SealedTreeError, match="symlink"):
            sealed.contract(repo)

    def test_sealed_guard_reports_an_absent_round_file_as_no_rounds(
        self, repo: Path
    ) -> None:
        (_missions_dir(repo) / CLOSED_MISSION / "results.yaml").unlink()
        (_missions_dir(repo) / CLOSED_MISSION / "reflections.yaml").unlink()
        sealed = MissionDirectory(
            _missions_dir(repo) / CLOSED_MISSION, guard=SealedTree(repo)
        )
        assert sealed.rounds() == ()
