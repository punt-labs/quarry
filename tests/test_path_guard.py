"""Behaviour of the symlink-refusing path guard and its trusting counterpart."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.path_guard import (
    FOLLOW_SYMLINKS,
    FollowSymlinks,
    PathGuard,
    SealedTree,
    SealedTreeError,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "file.yaml").write_text("k: v\n")
    return repo


class TestFollowSymlinks:
    def test_returns_the_path_unchanged(self, root: Path) -> None:
        link = root / "link"
        link.symlink_to(root / "a")
        assert FollowSymlinks().check(link) == link

    def test_module_default_is_the_trusting_guard(self) -> None:
        guard: PathGuard = FOLLOW_SYMLINKS
        assert isinstance(guard, FollowSymlinks)


class TestSealedTree:
    def test_plain_path_under_root_passes(self, root: Path) -> None:
        target = root / "a" / "b" / "file.yaml"
        assert SealedTree(root).check(target) == target

    def test_absent_leaf_passes(self, root: Path) -> None:
        """Absence is the caller's call; a missing component is not a symlink."""
        target = root / "a" / "b" / "missing.yaml"
        assert SealedTree(root).check(target) == target

    def test_root_property(self, root: Path) -> None:
        assert SealedTree(root).root == root

    def test_dot_dot_is_refused_before_touching_the_filesystem(
        self, root: Path
    ) -> None:
        with pytest.raises(SealedTreeError, match="escapes"):
            SealedTree(root).check(root / ".." / "other" / "file.yaml")

    def test_path_under_another_prefix_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(SealedTreeError, match="outside"):
            SealedTree(root).check(tmp_path / "elsewhere.yaml")

    def test_symlinked_leaf_is_refused(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.yaml"
        outside.write_text("k: v\n")
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        with pytest.raises(SealedTreeError, match="symlink"):
            SealedTree(root).check(link)

    def test_symlinked_intermediate_directory_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        """A real file below a symlinked directory still leaves the tree."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.yaml").write_text("k: v\n")
        (root / "a" / "escape").symlink_to(outside)
        target = root / "a" / "escape" / "file.yaml"
        assert not target.is_symlink()
        with pytest.raises(SealedTreeError, match="escape is a symlink"):
            SealedTree(root).check(target)

    def test_symlink_pointing_inside_the_tree_is_still_refused(
        self, root: Path
    ) -> None:
        """The rule is no symlink at all, not 'no symlink that leaves'."""
        (root / "a" / "alias").symlink_to(root / "a" / "b")
        with pytest.raises(SealedTreeError, match="symlink"):
            SealedTree(root).check(root / "a" / "alias" / "file.yaml")

    def test_symlinked_root_itself_is_trusted(self, root: Path, tmp_path: Path) -> None:
        """The operator chose the root; only components below it are sealed."""
        link_root = tmp_path / "via-link"
        link_root.symlink_to(root)
        target = link_root / "a" / "b" / "file.yaml"
        assert SealedTree(link_root).check(target) == target

    def test_error_is_an_os_error(self, root: Path) -> None:
        """Callers that fold I/O failures per item catch this the same way."""
        with pytest.raises(OSError, match="refused"):
            SealedTree(root).check(root / ".." / "x")
