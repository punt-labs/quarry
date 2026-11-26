"""Unit tests for :class:`~quarry.registered_root.RegisteredRoot`."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.registered_root import RegisteredRoot
from quarry.scratch_paths import ScratchGuard


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    (path / "open").mkdir(parents=True)
    (path / "open" / "a.md").write_text("a")
    return path


class TestAvailability:
    def test_an_existing_root_is_available(self, root: Path):
        assert RegisteredRoot(root).available

    def test_a_missing_root_is_not_available(self, tmp_path: Path):
        assert not RegisteredRoot(tmp_path / "missing").available

    def test_a_repo_scratch_root_is_not_available(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        scratch = tmp_path / ".tmp" / "docs"
        scratch.mkdir(parents=True)
        assert not RegisteredRoot(scratch).available

    def test_directory_and_guard_are_exposed(self, root: Path):
        registered = RegisteredRoot(root)
        assert registered.directory == root
        assert isinstance(registered.guard, ScratchGuard)


class TestRelative:
    def test_a_path_below_the_root_is_made_relative(self, root: Path):
        assert RegisteredRoot(root).relative(root / "open" / "a.md") == Path(
            "open/a.md"
        )

    def test_a_path_outside_the_root_raises(self, root: Path):
        with pytest.raises(ValueError, match="not in the subpath"):
            RegisteredRoot(root).relative(root.parent / "elsewhere.md")


class TestHiddenOrSkipped:
    @pytest.mark.parametrize(
        "rel", [".hidden/a.md", "x/.hidden.md", "x/._fork.md", "node_modules/a.md"]
    )
    def test_hidden_and_scratch_segments_are_flagged(self, root: Path, rel: str):
        assert RegisteredRoot(root).hidden_or_skipped(Path(rel))

    def test_an_ordinary_path_is_not_flagged(self, root: Path):
        assert not RegisteredRoot(root).hidden_or_skipped(Path("open/a.md"))


class TestCrossesSymlink:
    def test_plain_directories_do_not_cross(self, root: Path):
        assert not RegisteredRoot(root).crosses_symlink(("open",))

    def test_a_symlinked_directory_crosses(self, root: Path):
        (root / "link").symlink_to(root / "open")
        assert RegisteredRoot(root).crosses_symlink(("link",))

    def test_a_symlinked_ancestor_crosses(self, root: Path):
        (root / "link").symlink_to(root / "open")
        assert RegisteredRoot(root).crosses_symlink(("link", "deeper"))

    def test_no_parts_never_crosses(self, root: Path):
        assert not RegisteredRoot(root).crosses_symlink(())


class TestTargetParts:
    def test_a_regular_file_resolves_to_itself(self, root: Path):
        assert RegisteredRoot(root).target_parts(root / "open" / "a.md") == (
            "open",
            "a.md",
        )

    def test_a_link_resolves_to_its_target(self, root: Path):
        (root / "link.md").symlink_to(root / "open" / "a.md")
        assert RegisteredRoot(root).target_parts(root / "link.md") == ("open", "a.md")

    def test_a_link_leaving_the_root_raises(self, root: Path):
        outside = root.parent / "outside.md"
        outside.write_text("x")
        (root / "escape.md").symlink_to(outside)
        with pytest.raises(ValueError, match="outside the root"):
            RegisteredRoot(root).target_parts(root / "escape.md")

    def test_a_missing_file_raises(self, root: Path):
        with pytest.raises(ValueError, match="cannot resolve"):
            RegisteredRoot(root).target_parts(root / "gone.md")

    def test_a_loop_raises(self, root: Path):
        (root / "loop.md").symlink_to(root / "loop.md")
        with pytest.raises(ValueError, match="cannot resolve"):
            RegisteredRoot(root).target_parts(root / "loop.md")

    def test_an_unresolvable_root_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="not resolvable"):
            RegisteredRoot(tmp_path / "missing").target_parts(tmp_path / "a.md")
