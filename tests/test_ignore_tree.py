"""Unit tests for :class:`~quarry.ignore_tree.IgnoreTree`."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.ignore_tree import IgnoreTree


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestIgnores:
    def test_a_tree_without_ignore_files_applies_only_the_defaults(
        self, tmp_path: Path
    ):
        tree = IgnoreTree(tmp_path)
        assert tree.ignores(("a", "mod.pyc"), is_dir=False)
        assert not tree.ignores(("a", "mod.md"), is_dir=False)

    @pytest.mark.parametrize("name", [".gitignore", ".quarryignore"])
    def test_a_directory_file_matches_relative_to_its_own_directory(
        self, tmp_path: Path, name: str
    ):
        _write(tmp_path, f"child/{name}", "docs/private/\n")
        tree = IgnoreTree(tmp_path)
        assert tree.ignores(("child", "docs", "private"), is_dir=True)
        assert not tree.ignores(("child", "a", "docs", "private"), is_dir=True)
        assert not tree.ignores(("docs", "private"), is_dir=True)

    def test_an_unanchored_pattern_applies_at_every_depth_below_its_file(
        self, tmp_path: Path
    ):
        _write(tmp_path, "child/.gitignore", "secret.md\n")
        tree = IgnoreTree(tmp_path)
        assert tree.ignores(("child", "a", "b", "secret.md"), is_dir=False)
        assert not tree.ignores(("secret.md",), is_dir=False)

    def test_the_deepest_matching_file_wins_over_a_shallower_one(self, tmp_path: Path):
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, "a/.gitignore", "!x.md\n")
        _write(tmp_path, "a/b/.gitignore", "x.md\n")
        tree = IgnoreTree(tmp_path)
        assert tree.ignores(("y.md",), is_dir=False)
        assert not tree.ignores(("a", "x.md"), is_dir=False)
        assert tree.ignores(("a", "b", "x.md"), is_dir=False)
        assert tree.ignores(("a", "b", "c", "x.md"), is_dir=False)

    def test_a_deeper_file_without_a_match_defers_to_a_shallower_one(
        self, tmp_path: Path
    ):
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, "a/.gitignore", "other.txt\n")
        assert IgnoreTree(tmp_path).ignores(("a", "x.md"), is_dir=False)

    def test_both_files_of_one_directory_apply_with_quarryignore_last(
        self, tmp_path: Path
    ):
        _write(tmp_path, ".gitignore", "*.md\n")
        _write(tmp_path, ".quarryignore", "!keep.md\n")
        tree = IgnoreTree(tmp_path)
        assert not tree.ignores(("keep.md",), is_dir=False)
        assert tree.ignores(("drop.md",), is_dir=False)

    def test_a_user_rule_overrides_a_default(self, tmp_path: Path):
        _write(tmp_path, ".gitignore", "!mod.pyc\n")
        assert not IgnoreTree(tmp_path).ignores(("mod.pyc",), is_dir=False)

    def test_an_excluded_ancestor_is_the_callers_question_not_this_methods(
        self, tmp_path: Path
    ):
        _write(tmp_path, ".gitignore", "private/\n")
        tree = IgnoreTree(tmp_path)
        assert tree.ignores(("private",), is_dir=True)
        assert not tree.ignores(("private", "a.md"), is_dir=False)

    def test_the_root_itself_is_never_ignored(self, tmp_path: Path):
        _write(tmp_path, ".gitignore", "*\n")
        assert not IgnoreTree(tmp_path).ignores((), is_dir=True)


class TestCaching:
    def test_an_ignore_file_edited_after_first_use_is_not_re_read(self, tmp_path: Path):
        _write(tmp_path, "sub/.gitignore", "*.log\n")
        tree = IgnoreTree(tmp_path)
        assert tree.ignores(("sub", "a.log"), is_dir=False)
        (tmp_path / "sub" / ".gitignore").write_text("")
        assert tree.ignores(("sub", "b.log"), is_dir=False)

    def test_a_directory_without_ignore_files_is_cached_negatively(
        self, tmp_path: Path
    ):
        (tmp_path / "sub").mkdir()
        tree = IgnoreTree(tmp_path)
        assert not tree.ignores(("sub", "a.log"), is_dir=False)
        _write(tmp_path, "sub/.gitignore", "*.log\n")
        assert not tree.ignores(("sub", "b.log"), is_dir=False)

    def test_the_root_file_is_read_at_construction(self, tmp_path: Path):
        tree = IgnoreTree(tmp_path)
        _write(tmp_path, ".gitignore", "*.log\n")
        assert not tree.ignores(("a.log",), is_dir=False)
