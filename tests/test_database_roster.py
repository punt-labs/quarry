"""DatabaseRoster is the one rule for which database directories a daemon may open."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.database_roster import DatabaseRoster

if TYPE_CHECKING:
    from pathlib import Path


def _db(root: Path, name: str, *, registry: bool = False, serve: str = "") -> None:
    (root / name).mkdir(parents=True)
    if registry:
        (root / name / "registry.db").write_bytes(b"")
    if serve:
        (root / name / serve).write_bytes(b"")


class TestMembers:
    def test_a_directory_with_a_registry_is_a_member(self, tmp_path: Path) -> None:
        _db(tmp_path, "work", registry=True)
        assert DatabaseRoster(tmp_path).members() == ["work"]

    def test_a_directory_with_nothing_is_not(self, tmp_path: Path) -> None:
        _db(tmp_path, "empty")
        assert DatabaseRoster(tmp_path).members() == []

    def test_a_missing_root_has_no_members(self, tmp_path: Path) -> None:
        assert DatabaseRoster(tmp_path / "absent").members() == []

    def test_members_are_sorted(self, tmp_path: Path) -> None:
        for name in ("zed", "alpha", "mid"):
            _db(tmp_path, name, registry=True)
        assert DatabaseRoster(tmp_path).members() == ["alpha", "mid", "zed"]

    def test_a_plain_file_is_not_a_member(self, tmp_path: Path) -> None:
        (tmp_path / "stray").write_bytes(b"")
        assert DatabaseRoster(tmp_path).members() == []


class TestWithDaemonTraces:
    def test_a_lock_without_a_registry_still_counts(self, tmp_path: Path) -> None:
        _db(tmp_path, "fresh", serve="serve.lock")
        assert DatabaseRoster(tmp_path).with_daemon_traces() == ["fresh"]

    def test_a_port_without_a_registry_still_counts(self, tmp_path: Path) -> None:
        _db(tmp_path, "fresh", serve="serve.port")
        assert DatabaseRoster(tmp_path).with_daemon_traces() == ["fresh"]

    def test_registry_members_are_included_too(self, tmp_path: Path) -> None:
        _db(tmp_path, "work", registry=True)
        _db(tmp_path, "fresh", serve="serve.lock")
        assert DatabaseRoster(tmp_path).with_daemon_traces() == ["fresh", "work"]

    def test_a_directory_with_neither_is_left_out(self, tmp_path: Path) -> None:
        _db(tmp_path, "empty")
        assert DatabaseRoster(tmp_path).with_daemon_traces() == []
