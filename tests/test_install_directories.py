"""InstallDirectories: the install step names the directory it cannot make."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.config import Settings
from quarry.db_pointer import SELECTION
from quarry.install_directories import InstallDirectories
from tests.home_only import HomeOnly

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    MP = pytest.MonkeyPatch


class TestCreate:
    def test_a_directory_that_cannot_be_made_is_named(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blocker = tmp_path / "logs"
        blocker.write_text("a file, not a directory")
        monkeypatch.setenv("QUARRY_ROOT", str(tmp_path / "data"))
        monkeypatch.setenv("QUARRY_LOG_DIR", str(blocker))

        assert InstallDirectories.create() is False

        out = capsys.readouterr().out
        assert f"Failed to create the logs directory {blocker}:" in out
        assert f"✓ {tmp_path / 'data' / 'default' / 'lancedb'}" in out

    def test_both_directories_are_listed_when_both_are_made(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("QUARRY_ROOT", str(tmp_path / "data"))
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path / "logs"))

        assert InstallDirectories.create() is True

        assert capsys.readouterr().out.count("✓") == 2

    def test_a_data_directory_that_cannot_be_made_does_not_hide_the_logs(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blocker = tmp_path / "data"
        blocker.write_text("a file, not a directory")
        monkeypatch.setenv("QUARRY_ROOT", str(blocker))
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path / "logs"))

        assert InstallDirectories.create() is False

        out = capsys.readouterr().out
        assert (
            f"Failed to create the data directory {blocker / 'default' / 'lancedb'}:"
            in out
        )
        assert f"✓ {tmp_path / 'logs'}" in out


class TestCheck:
    def test_existing_writable_directory(self, tmp_path: Path, monkeypatch: MP):
        data_dir = tmp_path / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)
        monkeypatch.setenv("QUARRY_ROOT", str(tmp_path / "data"))
        result = InstallDirectories.check(Settings())
        assert result.passed is True
        assert result.message == str(data_dir)

    def test_missing_directory(self, tmp_path: Path, monkeypatch: MP):
        monkeypatch.setenv("QUARRY_ROOT", str(tmp_path / "data"))
        result = InstallDirectories.check(Settings())
        assert result.passed is False
        assert "does not exist" in result.message

    def test_the_check_follows_the_home_when_the_root_is_unset(
        self, tmp_path: Path, monkeypatch: MP
    ):
        HomeOnly.apply(monkeypatch, tmp_path)
        data_dir = tmp_path / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
        data_dir.mkdir(parents=True)

        assert InstallDirectories.check(Settings()).message == str(data_dir)

    def test_the_check_reads_the_selected_database(
        self, tmp_path: Path, monkeypatch: MP
    ):
        monkeypatch.setenv("QUARRY_ROOT", str(tmp_path / "data"))
        selected = tmp_path / "data" / "work" / "lancedb"
        selected.mkdir(parents=True)
        SELECTION.override("work")

        assert InstallDirectories.check(Settings()).message == str(selected)
