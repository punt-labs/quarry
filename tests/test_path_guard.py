"""Behaviour of the symlink-refusing path guard and its trusting counterpart."""

from __future__ import annotations

import stat
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


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A real file outside every sealed root, the target a hostile link aims at."""
    target = tmp_path / "outside.yaml"
    target.write_text("k: outside\n")
    return target


class TestFollowSymlinks:
    def test_returns_the_path_unchanged(self, root: Path) -> None:
        link = root / "link"
        link.symlink_to(root / "a")
        assert FollowSymlinks().check(link) == link

    def test_module_default_is_the_trusting_guard(self) -> None:
        guard: PathGuard = FOLLOW_SYMLINKS
        assert isinstance(guard, FollowSymlinks)

    def test_read_text_follows_a_symlink(self, root: Path, outside: Path) -> None:
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        assert FollowSymlinks().read_text(link) == "k: outside\n"

    def test_is_regular_file_follows_a_symlink(self, root: Path, outside: Path) -> None:
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        assert FollowSymlinks().is_regular_file(link) is True
        assert FollowSymlinks().is_regular_file(root / "a") is False
        assert FollowSymlinks().is_regular_file(root / "missing") is False


class TestFollowSymlinksWriteText:
    """The operator's own tree: a dotfile-manager link is written through, kept."""

    def test_writes_through_an_operator_symlink_keeping_link_and_mode(
        self, root: Path, outside: Path
    ) -> None:
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        outside.chmod(0o600)
        FollowSymlinks().write_text(link, "k: new\n")
        assert link.is_symlink()
        assert outside.read_text() == "k: new\n"
        assert stat.S_IMODE(outside.stat().st_mode) == 0o600


class TestSealedTreeCheck:
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

    def test_symlinked_leaf_is_refused(self, root: Path, outside: Path) -> None:
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


class TestSealedTreeReadText:
    """The read itself refuses a symlink: there is no check-then-read window."""

    def test_reads_a_real_file(self, root: Path) -> None:
        assert SealedTree(root).read_text(root / "a" / "b" / "file.yaml") == "k: v\n"

    def test_reads_through_a_symlinked_root(self, root: Path, tmp_path: Path) -> None:
        link_root = tmp_path / "via-link"
        link_root.symlink_to(root)
        target = link_root / "a" / "b" / "file.yaml"
        assert SealedTree(link_root).read_text(target) == "k: v\n"

    def test_symlinked_leaf_is_refused(self, root: Path, outside: Path) -> None:
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        with pytest.raises(SealedTreeError, match="symlink"):
            SealedTree(root).read_text(link)

    def test_symlinked_intermediate_directory_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        escape = tmp_path / "escape"
        escape.mkdir()
        (escape / "file.yaml").write_text("k: outside\n")
        (root / "a" / "escape").symlink_to(escape)
        with pytest.raises(SealedTreeError, match="symlink"):
            SealedTree(root).read_text(root / "a" / "escape" / "file.yaml")

    def test_component_swapped_to_a_symlink_after_check_is_still_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        """A check that passed grants nothing: the read re-walks with O_NOFOLLOW.

        This is the race a check-then-read had: the directory is real when
        checked and a symlink by the time the file is opened.
        """
        escape = tmp_path / "escape"
        escape.mkdir()
        (escape / "file.yaml").write_text("k: outside\n")
        target = root / "a" / "b" / "file.yaml"
        seal = SealedTree(root)
        assert seal.check(target) == target
        (root / "a" / "b").rename(tmp_path / "moved")
        (root / "a" / "b").symlink_to(escape)
        with pytest.raises(SealedTreeError, match="symlink"):
            seal.read_text(target)

    def test_dot_dot_is_refused(self, root: Path) -> None:
        with pytest.raises(SealedTreeError, match="escapes"):
            SealedTree(root).read_text(root / "a" / ".." / ".." / "x.yaml")

    def test_path_outside_the_root_is_refused(self, root: Path, outside: Path) -> None:
        with pytest.raises(SealedTreeError, match="outside"):
            SealedTree(root).read_text(outside)

    def test_absent_file_is_file_not_found(self, root: Path) -> None:
        with pytest.raises(FileNotFoundError):
            SealedTree(root).read_text(root / "a" / "b" / "missing.yaml")

    def test_the_root_itself_is_not_a_readable_file(self, root: Path) -> None:
        with pytest.raises(SealedTreeError, match="refused"):
            SealedTree(root).read_text(root)


class TestSealedTreeWriteText:
    """The write itself refuses a symlink: there is no check-then-write window."""

    def test_rewrites_a_real_file_keeping_its_mode_and_no_temp(
        self, root: Path
    ) -> None:
        target = root / "a" / "b" / "file.yaml"
        target.chmod(0o600)
        SealedTree(root).write_text(target, "k: new\n")
        assert target.read_text() == "k: new\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert [p.name for p in target.parent.iterdir()] == ["file.yaml"]

    def test_creates_an_absent_leaf_under_a_real_directory(self, root: Path) -> None:
        target = root / "a" / "b" / "new.yaml"
        SealedTree(root).write_text(target, "k: v\n")
        assert target.read_text() == "k: v\n"

    def test_symlinked_leaf_is_refused_and_left_as_found(
        self, root: Path, outside: Path
    ) -> None:
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        with pytest.raises(SealedTreeError, match="refused"):
            SealedTree(root).write_text(link, "k: clobber\n")
        assert outside.read_text() == "k: outside\n"
        assert link.is_symlink()

    def test_symlinked_intermediate_directory_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        escape = tmp_path / "escape"
        escape.mkdir()
        (escape / "file.yaml").write_text("k: outside\n")
        (root / "a" / "escape").symlink_to(escape)
        with pytest.raises(SealedTreeError, match="symlink"):
            SealedTree(root).write_text(
                root / "a" / "escape" / "file.yaml", "k: clobber\n"
            )
        assert (escape / "file.yaml").read_text() == "k: outside\n"
        assert list(escape.iterdir()) == [escape / "file.yaml"]

    def test_component_swapped_to_a_symlink_after_check_is_still_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        """A check that passed grants nothing: the write re-walks with O_NOFOLLOW."""
        escape = tmp_path / "escape"
        escape.mkdir()
        (escape / "file.yaml").write_text("k: outside\n")
        target = root / "a" / "b" / "file.yaml"
        seal = SealedTree(root)
        assert seal.check(target) == target
        (root / "a" / "b").rename(tmp_path / "moved")
        (root / "a" / "b").symlink_to(escape)
        with pytest.raises(SealedTreeError, match="symlink"):
            seal.write_text(target, "k: clobber\n")
        assert (escape / "file.yaml").read_text() == "k: outside\n"
        assert list(escape.iterdir()) == [escape / "file.yaml"]

    def test_dot_dot_is_refused(self, root: Path) -> None:
        with pytest.raises(SealedTreeError, match="escapes"):
            SealedTree(root).write_text(root / "a" / ".." / ".." / "x.yaml", "k: v\n")

    def test_path_outside_the_root_is_refused(self, root: Path, outside: Path) -> None:
        with pytest.raises(SealedTreeError, match="outside"):
            SealedTree(root).write_text(outside, "k: clobber\n")
        assert outside.read_text() == "k: outside\n"

    def test_the_root_itself_is_not_a_writable_file(self, root: Path) -> None:
        with pytest.raises(SealedTreeError, match="refused"):
            SealedTree(root).write_text(root, "k: v\n")


class TestSealedTreeIsRegularFile:
    def test_true_for_a_real_file(self, root: Path) -> None:
        assert SealedTree(root).is_regular_file(root / "a" / "b" / "file.yaml")

    def test_false_for_a_symlinked_leaf(self, root: Path, outside: Path) -> None:
        link = root / "a" / "b" / "link.yaml"
        link.symlink_to(outside)
        assert link.is_file()  # what a naive probe would say
        assert SealedTree(root).is_regular_file(link) is False

    def test_false_below_a_symlinked_directory(
        self, root: Path, tmp_path: Path
    ) -> None:
        escape = tmp_path / "escape"
        escape.mkdir()
        (escape / "file.yaml").write_text("k: outside\n")
        (root / "a" / "escape").symlink_to(escape)
        below_link = root / "a" / "escape" / "file.yaml"
        assert SealedTree(root).is_regular_file(below_link) is False

    def test_false_for_a_directory_and_for_an_absent_path(self, root: Path) -> None:
        assert SealedTree(root).is_regular_file(root / "a") is False
        assert SealedTree(root).is_regular_file(root / "a" / "nope.yaml") is False

    def test_outside_the_root_is_refused_not_false(
        self, root: Path, outside: Path
    ) -> None:
        """A caller asking about a path outside its seal has a bug, not a miss."""
        with pytest.raises(SealedTreeError, match="outside"):
            SealedTree(root).is_regular_file(outside)
