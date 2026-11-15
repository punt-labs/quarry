"""Tests for :mod:`quarry.project_config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.project_config import ProjectConfig


class TestProjectConfig:
    def test_creates_config_with_template(self, tmp_path: Path) -> None:
        result_path = ProjectConfig().write(tmp_path)
        config = Path(result_path)
        assert config.exists()
        assert config.read_text() == ProjectConfig().text

    def test_idempotent_no_overwrite(self, tmp_path: Path) -> None:
        ProjectConfig().write(tmp_path)
        config = tmp_path / ".punt-labs" / "quarry" / "config.md"
        config.write_text("custom content")
        ProjectConfig().write(tmp_path)
        assert config.read_text() == "custom content"

    def test_atomic_no_overwrite_existing(self, tmp_path: Path) -> None:
        """Verify O_CREAT|O_EXCL path: pre-existing file is never opened for write."""
        config_dir = tmp_path / ".punt-labs" / "quarry"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.md"
        original = "do not touch\n"
        config_path.write_text(original)

        ProjectConfig().write(tmp_path)

        assert config_path.read_text() == original

    def test_refuses_symlinked_ancestor(self, tmp_path: Path) -> None:
        """A symlinked .punt-labs ancestor makes the config write refuse, not escape."""
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (repo / ".punt-labs").symlink_to(external)

        with pytest.raises(ValueError, match="ancestor"):
            ProjectConfig().write(repo)

        assert not (external / "quarry" / "config.md").exists()
