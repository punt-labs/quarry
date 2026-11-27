"""Tests for removing a file where an unsearchable directory reads as absent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from quarry.config_discard import ConfigDiscard

if TYPE_CHECKING:
    from collections.abc import Iterator

_ROOT_SKIP = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory modes"
)


def _config(root: Path) -> Path:
    path = root / ".punt-labs" / "quarry" / "config.md"
    path.parent.mkdir(parents=True)
    path.write_text("x")
    return path


@pytest.fixture
def locked() -> Iterator[list[tuple[Path, int]]]:
    """Collect (directory, mode) pairs to apply; restore 0o755 on teardown."""
    touched: list[tuple[Path, int]] = []
    yield touched
    for directory, _ in touched:
        directory.chmod(0o755)


def test_a_present_file_is_removed(tmp_path: Path) -> None:
    path = _config(tmp_path)
    assert ConfigDiscard(tmp_path).remove() is True
    assert not path.exists()


def test_an_absent_file_is_not_removed(tmp_path: Path) -> None:
    assert ConfigDiscard(tmp_path).remove() is False


def test_a_symlinked_ancestor_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    path = _config(outside)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".punt-labs").symlink_to(outside / ".punt-labs")
    with pytest.raises(ValueError, match="symlinked"):
        ConfigDiscard(repo).remove()
    assert path.exists()


@_ROOT_SKIP
def test_a_directory_without_search_permission_reads_as_absent(
    tmp_path: Path, locked: list[tuple[Path, int]]
) -> None:
    path = _config(tmp_path)
    locked.append((path.parent, 0o400))
    path.parent.chmod(0o400)
    assert ConfigDiscard(tmp_path).remove() is False
    path.parent.chmod(0o755)
    assert path.exists()


@_ROOT_SKIP
@pytest.mark.parametrize("level", [".punt-labs", ".punt-labs/quarry"])
def test_a_directory_that_cannot_be_entered_is_refused_like_a_symlink(
    tmp_path: Path, locked: list[tuple[Path, int]], level: str
) -> None:
    path = _config(tmp_path)
    directory = tmp_path / level
    locked.append((directory, 0o000))
    directory.chmod(0o000)
    with pytest.raises(ValueError, match="symlinked or non-directory"):
        ConfigDiscard(tmp_path).remove()
    directory.chmod(0o755)
    assert path.exists()


@_ROOT_SKIP
def test_a_failed_unlink_of_a_present_file_propagates(
    tmp_path: Path, locked: list[tuple[Path, int]]
) -> None:
    path = _config(tmp_path)
    locked.append((path.parent, 0o555))
    path.parent.chmod(0o555)
    with pytest.raises(PermissionError):
        ConfigDiscard(tmp_path).remove()
    assert path.exists()
