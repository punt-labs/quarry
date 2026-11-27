"""Tests for the local-file steps of a disable: config disposal and the guard."""

from __future__ import annotations

from pathlib import Path

from quarry.enabled_marker import EnabledMarker
from quarry.guidance import REPO_IMPORT_LINE
from quarry.local_disable import ConfigDisposal, GuardedDisable
from quarry.project_config import ProjectConfig


def _write(root: Path, text: str) -> Path:
    path = root / ".punt-labs" / "quarry" / "config.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_covered_template_config_is_removed(tmp_path: Path) -> None:
    path = _write(tmp_path, ProjectConfig().text)
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=True, kept=False
    )
    assert not path.exists()


def test_covered_customised_config_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "---\nauto_capture:\n  session_end: false\n---\n")
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=False, kept=True
    )
    assert path.exists()


def test_covered_unreadable_config_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    path.write_bytes(b"\xff\xfe\x00bad")
    assert ConfigDisposal.settle(tmp_path, covered=True).kept is True
    assert path.exists()


def test_uncovered_customised_config_is_removed(tmp_path: Path) -> None:
    path = _write(tmp_path, "---\nauto_capture:\n  session_end: false\n---\n")
    assert ConfigDisposal.settle(tmp_path, covered=False) == ConfigDisposal(
        removed=True, kept=False
    )
    assert not path.exists()


def test_absent_config_is_neither_removed_nor_kept(tmp_path: Path) -> None:
    for covered in (True, False):
        assert ConfigDisposal.settle(tmp_path, covered=covered) == ConfigDisposal(
            removed=False, kept=False
        )


def test_symlinked_ancestor_counts_as_absent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "x")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".punt-labs").symlink_to(outside / ".punt-labs")
    assert ConfigDisposal.settle(repo, covered=False) == ConfigDisposal(
        removed=False, kept=False
    )
    assert (outside / ".punt-labs" / "quarry" / "config.md").exists()


def test_guard_empty_repo_has_nothing_to_remove(tmp_path: Path) -> None:
    assert GuardedDisable(tmp_path).present() is False


def test_claude_md_without_the_import_has_nothing(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    assert GuardedDisable(tmp_path).present() is False


def test_marker_alone_counts(tmp_path: Path) -> None:
    EnabledMarker(tmp_path).write()
    assert GuardedDisable(tmp_path).present() is True


def test_import_alone_counts(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(f"{REPO_IMPORT_LINE}\n")
    assert GuardedDisable(tmp_path).present() is True


def test_unreadable_claude_md_errs_toward_present(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe\x00")
    assert GuardedDisable(tmp_path).present() is True
