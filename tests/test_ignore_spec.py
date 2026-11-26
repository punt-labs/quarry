"""Unit tests for :class:`~quarry.ignore_spec.IgnoreRules`.

The behavior is also exercised through
:class:`~quarry.sync_discovery.FileDiscovery` in ``tests/test_sync.py`` and
against real git in ``tests/test_ignore_git_reference.py``; these tests pin the
class's own contract directly so it stays correct even if ``FileDiscovery``
stops calling one of these methods.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.ignore_spec import IgnoreRules
from quarry.ignore_tree import _DEFAULT_IGNORE_PATTERNS
from quarry.scratch_paths import ScratchGuard


def _rules(root: Path) -> IgnoreRules:
    return IgnoreRules(root, ScratchGuard())


class TestKeeps:
    def test_the_root_is_always_kept(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*\n")
        assert _rules(tmp_path).keeps((), is_dir=True)

    def test_plain_names_are_kept(self, tmp_path: Path):
        rules = _rules(tmp_path)
        assert rules.keeps(("src",), is_dir=True)
        assert rules.keeps(("src", "app.md"), is_dir=False)

    def test_no_ignore_files_applies_the_defaults(self, tmp_path: Path):
        rules = _rules(tmp_path)
        assert not rules.keeps(("module.pyc",), is_dir=False)
        assert not rules.keeps(("deep", "module.pyc"), is_dir=False)

    @pytest.mark.parametrize("is_dir", [True, False])
    def test_hidden_names_are_dropped(self, tmp_path: Path, *, is_dir: bool):
        assert not _rules(tmp_path).keeps((".hidden",), is_dir=is_dir)

    def test_a_hidden_ancestor_drops_everything_below(self, tmp_path: Path):
        assert not _rules(tmp_path).keeps((".hidden", "a.md"), is_dir=False)

    def test_a_scratch_directory_name_is_dropped(self, tmp_path: Path):
        rules = _rules(tmp_path)
        assert not rules.keeps(("node_modules",), is_dir=True)
        assert not rules.keeps(("node_modules", "pkg", "a.md"), is_dir=False)

    def test_a_scratch_name_is_not_applied_to_a_file(self, tmp_path: Path):
        assert _rules(tmp_path).keeps(("venv",), is_dir=False)

    @pytest.mark.parametrize("ignore_name", [".gitignore", ".quarryignore"])
    def test_an_ignore_file_matches_below_its_own_directory(
        self, tmp_path: Path, ignore_name: str
    ):
        (tmp_path / "child").mkdir()
        (tmp_path / "child" / ignore_name).write_text("secret.md\n")
        rules = _rules(tmp_path)
        assert not rules.keeps(("child", "secret.md"), is_dir=False)
        assert not rules.keeps(("child", "a", "b", "secret.md"), is_dir=False)
        assert rules.keeps(("secret.md",), is_dir=False)

    def test_both_ignore_files_in_a_directory_apply(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("a.md\n")
        (tmp_path / ".quarryignore").write_text("b.md\n")
        rules = _rules(tmp_path)
        assert not rules.keeps(("a.md",), is_dir=False)
        assert not rules.keeps(("b.md",), is_dir=False)

    def test_quarryignore_is_read_after_gitignore_so_it_can_re_include(
        self, tmp_path: Path
    ):
        (tmp_path / ".gitignore").write_text("*.md\n")
        (tmp_path / ".quarryignore").write_text("!keep.md\n")
        rules = _rules(tmp_path)
        assert rules.keeps(("keep.md",), is_dir=False)
        assert not rules.keeps(("drop.md",), is_dir=False)

    def test_a_user_rule_can_re_include_a_default_ignored_name(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("!module.pyc\n")
        assert _rules(tmp_path).keeps(("module.pyc",), is_dir=False)

    def test_nothing_below_an_excluded_directory_is_re_included(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("private/\n!private/open.md\n")
        assert not _rules(tmp_path).keeps(("private", "open.md"), is_dir=False)

    def test_an_ignore_file_inside_an_excluded_directory_is_not_consulted(
        self, tmp_path: Path
    ):
        (tmp_path / "private").mkdir()
        (tmp_path / ".gitignore").write_text("private/\n")
        (tmp_path / "private" / ".gitignore").write_text("!open.md\n")
        assert not _rules(tmp_path).keeps(("private", "open.md"), is_dir=False)


class TestMalformedInput:
    def test_a_trailing_backslash_is_skipped_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One bad line must not crash the daemon, and every OTHER pattern in
        the same file must still apply.
        """
        (tmp_path / ".gitignore").write_text("*.log\ntrailing\\\ndata/\n")
        rules = _rules(tmp_path)  # must not raise
        assert not rules.keeps(("debug.log",), is_dir=False)
        assert not rules.keeps(("data",), is_dir=True)
        assert any(
            "Skipping invalid ignore pattern" in r.message for r in caplog.records
        )

    def test_a_non_utf8_line_does_not_disable_the_other_rules(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".gitignore").write_bytes(b"\xff\xfe\n*.log\n")
        rules = _rules(tmp_path)  # must not raise
        assert not rules.keeps(("debug.log",), is_dir=False)
        assert not rules.keeps(("module.pyc",), is_dir=False)

    def test_a_nested_malformed_line_is_skipped_not_raised(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".gitignore").write_text("*.log\nbad\\\n")
        rules = _rules(tmp_path)
        assert not rules.keeps(("sub", "a.log"), is_dir=False)


class TestCaching:
    """Ignore files are read once per instance; edits apply to the NEXT one."""

    def test_a_root_ignore_file_written_after_construction_is_not_picked_up(
        self, tmp_path: Path
    ):
        rules = _rules(tmp_path)
        (tmp_path / ".gitignore").write_text("*.log\n")
        assert rules.keeps(("debug.log",), is_dir=False)

    def test_a_nested_ignore_file_written_after_first_use_is_not_picked_up(
        self, tmp_path: Path
    ):
        (tmp_path / "sub").mkdir()
        rules = _rules(tmp_path)
        assert rules.keeps(("sub", "a.log"), is_dir=False)
        (tmp_path / "sub" / ".gitignore").write_text("*.log\n")
        assert rules.keeps(("sub", "b.log"), is_dir=False)

    def test_a_directory_verdict_is_cached(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("out/\n")
        rules = _rules(tmp_path)
        assert not rules.keeps(("out", "a.md"), is_dir=False)
        (tmp_path / ".gitignore").write_text("")
        assert not rules.keeps(("out", "b.md"), is_dir=False)


def test_default_patterns_are_globs_not_dir_names():
    # Glob patterns stay in the defaults; scratch/VCS/cache DIR names live in
    # ScratchGuard (pruned by name), so they are not duplicated here.
    assert "*.pyc" in _DEFAULT_IGNORE_PATTERNS
    assert ".DS_Store" in _DEFAULT_IGNORE_PATTERNS
    assert "node_modules/" not in _DEFAULT_IGNORE_PATTERNS
    assert "venv/" not in _DEFAULT_IGNORE_PATTERNS
