"""Tests for judging a project's config.md by what the hooks do with it."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.config_verdict import ConfigVerdict
from quarry.project_config import ProjectConfig


def _config(root: Path) -> Path:
    path = root / ".punt-labs" / "quarry" / "config.md"
    path.parent.mkdir(parents=True)
    return path


def test_no_file_is_absent(tmp_path: Path) -> None:
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.ABSENT
    assert ConfigVerdict.ABSENT.left_in_place is False


def test_the_current_template_is_default(tmp_path: Path) -> None:
    _config(tmp_path).write_text(ProjectConfig().text)
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.DEFAULT


def test_a_file_with_no_auto_capture_block_is_default(tmp_path: Path) -> None:
    _config(tmp_path).write_text("# my notes\n")
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.DEFAULT


def test_an_opt_out_is_customised(tmp_path: Path) -> None:
    _config(tmp_path).write_text("---\nauto_capture:\n  web_fetch: false\n---\n")
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.CUSTOMISED
    assert ConfigVerdict.CUSTOMISED.left_in_place is True


def test_undecodable_text_is_unreadable(tmp_path: Path) -> None:
    _config(tmp_path).write_bytes(b"\xff\xfe\x00bad")
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.UNREADABLE


def test_a_directory_at_the_path_is_absent(tmp_path: Path) -> None:
    _config(tmp_path).mkdir()
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.ABSENT


def test_a_symlink_to_a_file_is_linked(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text(ProjectConfig().text)
    _config(tmp_path).symlink_to(real)
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.LINKED


def test_a_dangling_symlink_is_absent(tmp_path: Path) -> None:
    _config(tmp_path).symlink_to(tmp_path / "missing.md")
    assert ConfigVerdict.of(tmp_path) is ConfigVerdict.ABSENT


def test_discard_removes_a_regular_file_only(tmp_path: Path) -> None:
    path = _config(tmp_path)
    path.write_text("x")
    assert ConfigVerdict.discard(tmp_path) is True
    assert not path.exists()
    assert ConfigVerdict.discard(tmp_path) is False


def test_discard_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _config(outside).write_text("x")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".punt-labs").symlink_to(outside / ".punt-labs")
    with pytest.raises(ValueError, match="symlinked"):
        ConfigVerdict.discard(repo)
    assert (outside / ".punt-labs" / "quarry" / "config.md").exists()
