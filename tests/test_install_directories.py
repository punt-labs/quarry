"""InstallDirectories: the install step names the directory it cannot make."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.install_directories import InstallDirectories

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
        assert f"Failed to create the logs directory {blocker}" in out
        assert f"✓ {tmp_path / 'data' / 'default' / 'lancedb'}" in out

    def test_both_directories_are_listed_when_both_are_made(
        self, tmp_path: Path, monkeypatch: MP, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("QUARRY_ROOT", str(tmp_path / "data"))
        monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path / "logs"))

        assert InstallDirectories.create() is True

        assert capsys.readouterr().out.count("✓") == 2
