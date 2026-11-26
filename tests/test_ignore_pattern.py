"""Unit tests for :mod:`quarry.ignore_pattern` (git's ignore-pattern language)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.ignore_pattern import IgnoreFile, IgnorePattern


def _hits(line: str, rel: str, *, is_dir: bool = False) -> bool:
    pattern = IgnorePattern.parse(line)
    assert pattern is not None
    return pattern.matches(rel, is_dir=is_dir)


class TestParse:
    @pytest.mark.parametrize("line", ["", "   ", "# comment", "/", "!", "//"])
    def test_blank_comment_and_empty_lines_define_no_pattern(self, line: str):
        assert IgnorePattern.parse(line) is None

    def test_trailing_backslash_is_rejected(self):
        with pytest.raises(ValueError, match="backslash"):
            IgnorePattern.parse("name\\")

    def test_leading_bang_negates(self):
        pattern = IgnorePattern.parse("!keep.md")
        assert pattern is not None
        assert pattern.negated

    def test_escaped_bang_and_hash_are_literal(self):
        assert _hits("\\!x.md", "!x.md")
        assert _hits("\\#x.md", "#x.md")

    def test_trailing_spaces_are_dropped_unless_escaped(self):
        assert _hits("a.md  ", "a.md")
        assert _hits("a.md\\ ", "a.md ")


class TestMatching:
    def test_a_slashless_pattern_matches_the_name_at_any_depth(self):
        assert _hits("x.md", "x.md")
        assert _hits("x.md", "a/b/x.md")
        assert not _hits("x.md", "ax.md")

    def test_a_pattern_with_a_slash_is_anchored(self):
        assert _hits("a/x.md", "a/x.md")
        assert not _hits("a/x.md", "b/a/x.md")

    def test_a_leading_slash_anchors(self):
        assert _hits("/x.md", "x.md")
        assert not _hits("/x.md", "a/x.md")

    def test_a_trailing_slash_matches_directories_only(self):
        assert _hits("out/", "out", is_dir=True)
        assert not _hits("out/", "out", is_dir=False)

    def test_the_pattern_matches_the_path_itself_not_its_children(self):
        assert not _hits("out", "out/a.md")

    def test_star_does_not_cross_a_slash(self):
        assert _hits("*.md", "a.md")
        assert not _hits("a/*.md", "a/b/c.md")

    def test_question_mark_is_one_non_slash_character(self):
        assert _hits("q?.md", "q1.md")
        assert not _hits("q?.md", "q12.md")

    def test_leading_double_star_spans_directories(self):
        assert _hits("**/gen/a.md", "gen/a.md")
        assert _hits("**/gen/a.md", "x/y/gen/a.md")

    def test_middle_double_star_matches_zero_or_more_directories(self):
        assert _hits("a/**/z.md", "a/z.md")
        assert _hits("a/**/z.md", "a/b/c/z.md")
        assert not _hits("a/**/z.md", "b/z.md")

    def test_trailing_double_star_matches_everything_inside(self):
        assert _hits("tail/**", "tail/a.md")
        assert _hits("tail/**", "tail/x/y.md")
        assert not _hits("tail/**", "tail")

    def test_a_double_star_inside_a_name_is_an_ordinary_star(self):
        assert _hits("a**b.md", "axxb.md")
        assert not _hits("a**b.md", "ax/xb.md")

    @pytest.mark.parametrize(
        ("line", "rel", "expected"),
        [
            ("n[0-9].md", "n7.md", True),
            ("n[0-9].md", "nx.md", False),
            ("[!a-c]z.md", "dz.md", True),
            ("[!a-c]z.md", "az.md", False),
            ("[^a-c]z.md", "az.md", False),
            ("[[:digit:]].md", "5.md", True),
            ("[[:digit:]].md", "x.md", False),
            ("[]]x.md", "]x.md", True),
            ("[a\\]]x.md", "]x.md", True),
            ("[a-]x.md", "-x.md", True),
            ("[unterminated.md", "[unterminated.md", True),
        ],
    )
    def test_character_classes(self, line: str, rel: str, *, expected: bool):
        assert _hits(line, rel) is expected

    def test_a_class_never_matches_a_slash(self):
        assert not _hits("a[/]b", "a/b")


class TestIgnoreFile:
    def test_no_match_returns_none(self):
        assert IgnoreFile.from_lines(["*.log"]).verdict("a.md", is_dir=False) is None

    def test_a_match_returns_true(self):
        assert IgnoreFile.from_lines(["*.md"]).verdict("a.md", is_dir=False) is True

    def test_the_last_matching_pattern_wins(self):
        lines = ["*.md", "!keep.md", "keep.md"]
        assert IgnoreFile.from_lines(lines).verdict("keep.md", is_dir=False) is True
        lines = ["*.md", "keep.md", "!keep.md"]
        assert IgnoreFile.from_lines(lines).verdict("keep.md", is_dir=False) is False

    def test_a_malformed_line_does_not_drop_its_neighbours(self):
        file = IgnoreFile.from_lines(["a.md", "bad\\", "b.md"])
        assert file.verdict("a.md", is_dir=False) is True
        assert file.verdict("b.md", is_dir=False) is True


class TestLoad:
    def test_a_directory_without_ignore_files_has_none(self, tmp_path: Path):
        assert IgnoreFile.load(tmp_path) is None

    def test_both_files_load_with_the_later_one_overriding(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.md\n")
        (tmp_path / ".quarryignore").write_text("!keep.md\n")
        loaded = IgnoreFile.load(tmp_path)
        assert loaded is not None
        assert loaded.verdict("keep.md", is_dir=False) is False
        assert loaded.verdict("drop.md", is_dir=False) is True

    def test_defaults_come_first(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("!a.pyc\n")
        loaded = IgnoreFile.load(tmp_path, ["*.pyc"])
        assert loaded is not None
        assert loaded.verdict("a.pyc", is_dir=False) is False
        assert loaded.verdict("b.pyc", is_dir=False) is True

    def test_a_directory_named_gitignore_is_ignored_not_raised(self, tmp_path: Path):
        (tmp_path / ".gitignore").mkdir()
        assert IgnoreFile.load(tmp_path) is None

    def test_a_non_utf8_file_is_skipped(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_bytes(b"\xff\xfe*.log\n")
        assert IgnoreFile.load(tmp_path) is None
