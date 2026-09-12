"""Unit tests for :class:`~quarry.ignore_spec.IgnoreRules`.

The behavior is also exercised indirectly through
:class:`~quarry.sync_discovery.FileDiscovery` in ``tests/test_sync.py``; these
tests pin the class's own contract directly so it stays correct even if
``FileDiscovery`` stops calling one of these methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from quarry.ignore_spec import _DEFAULT_IGNORE_PATTERNS, IgnoreRules
from quarry.scratch_paths import ScratchGuard

if TYPE_CHECKING:
    import pytest


class TestRootSpec:
    def test_no_ignore_files_uses_defaults(self, tmp_path: Path):
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()
        assert spec.match_file("module.pyc")
        assert spec.match_file(".DS_Store")
        assert not spec.match_file("src/app.py")

    def test_loads_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\noutput/\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()
        assert spec.match_file("debug.log")
        assert spec.match_file("output/")
        assert not spec.match_file("app.py")

    def test_loads_quarryignore(self, tmp_path: Path):
        (tmp_path / ".quarryignore").write_text("scratch/\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()
        assert spec.match_file("scratch/")

    def test_comments_and_blanks_ignored(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("# comment\n\n*.log\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()
        assert spec.match_file("debug.log")
        assert not spec.match_file("# comment")

    def test_a_malformed_line_is_skipped_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bare '!' is an invalid gitignore pattern (pathspec's
        GitIgnorePatternError, a ValueError) -- construction must not
        crash the daemon over one bad line, and every OTHER pattern in the
        same file must still compile and match (djb/rev-silent finding).
        """
        (tmp_path / ".gitignore").write_text("*.log\n!\ndata/\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()  # must not raise
        assert spec.match_file("debug.log")
        assert spec.match_file("data/")
        assert any(
            "Skipping invalid ignore pattern" in r.message for r in caplog.records
        )

    def test_a_trailing_bare_backslash_is_skipped_not_raised(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".gitignore").write_text("*.log\ntrailing\\\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()  # must not raise
        assert spec.match_file("debug.log")

    def test_a_non_utf8_gitignore_is_skipped_not_raised(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_bytes(b"\xff\xfe*.log\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()  # must not raise
        assert not spec.match_file("debug.log")  # the whole file was skipped
        assert spec.match_file("module.pyc")  # built-in defaults still apply


class TestRootSpecCaching:
    """``root_spec`` is computed ONCE, at construction, and never re-read."""

    def test_root_spec_is_the_same_object_across_calls(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        assert rules.root_spec() is rules.root_spec()

    def test_a_gitignore_written_after_construction_is_not_picked_up(
        self, tmp_path: Path
    ):
        """Next-session-only semantics: mid-session ignore-file edits are inert
        until the next registration/daemon restart rebuilds ``IgnoreRules``.
        """
        rules = IgnoreRules(tmp_path, ScratchGuard())
        assert not rules.root_spec().match_file("debug.log")
        (tmp_path / ".gitignore").write_text("*.log\n")
        assert not rules.root_spec().match_file("debug.log")


class TestLocalSpec:
    def test_root_directory_has_no_local_spec(self, tmp_path: Path):
        """The root's own .gitignore is folded into root_spec, not local_spec."""
        (tmp_path / ".gitignore").write_text("*.log\n")
        assert IgnoreRules(tmp_path, ScratchGuard()).local_spec(tmp_path) is None

    def test_subdirectory_without_gitignore_has_no_local_spec(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert IgnoreRules(tmp_path, ScratchGuard()).local_spec(sub) is None

    def test_subdirectory_gitignore_becomes_local_spec(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("*.tmp\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).local_spec(sub)
        assert spec is not None
        assert spec.match_file("scratch.tmp")
        assert not spec.match_file("keep.py")

    def test_a_malformed_nested_gitignore_line_is_skipped_not_raised(
        self, tmp_path: Path
    ) -> None:
        """A bad line in a NESTED .gitignore, compiled lazily by
        ``local_spec`` on whatever thread first asks for it (the watchdog
        buffer thread, via ``PrunedInotify._add_watch``), must not raise --
        that thread has no reader to recover it (djb/rev-silent finding).
        """
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("*.tmp\n!\nkeep_out/\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).local_spec(sub)  # must not raise
        assert spec is not None
        assert spec.match_file("scratch.tmp")
        assert spec.match_file("keep_out/")


class TestLocalSpecCaching:
    """``local_spec`` is cached per directory the first time it is asked for,
    including a NEGATIVE (``None``) result -- a directory found to have no
    ``.gitignore`` on the first call must not be re-read on a later call.
    """

    def test_result_is_cached_across_calls(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("*.tmp\n")
        rules = IgnoreRules(tmp_path, ScratchGuard())
        assert rules.local_spec(sub) is rules.local_spec(sub)

    def test_a_gitignore_written_after_the_first_call_is_not_picked_up(
        self, tmp_path: Path
    ):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("*.tmp\n")
        rules = IgnoreRules(tmp_path, ScratchGuard())
        first = rules.local_spec(sub)
        assert first is not None
        assert first.match_file("scratch.tmp")
        (sub / ".gitignore").write_text("*.other\n")
        second = rules.local_spec(sub)
        assert second is not None
        assert second is first
        assert second.match_file("scratch.tmp")
        assert not second.match_file("scratch.other")

    def test_a_none_result_is_cached_negatively(self, tmp_path: Path):
        """A directory with no .gitignore caches None -- a .gitignore written
        afterward is not picked up until the next session (same rule as the
        positive case, just the absent-file branch of the same cache).
        """
        sub = tmp_path / "sub"
        sub.mkdir()
        rules = IgnoreRules(tmp_path, ScratchGuard())
        assert rules.local_spec(sub) is None
        (sub / ".gitignore").write_text("*.tmp\n")
        assert rules.local_spec(sub) is None


class TestKeepsDir:
    def test_plain_name_is_kept(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_dir(Path(), "src", root_spec, None) is True

    def test_hidden_name_is_pruned(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_dir(Path(), ".git", root_spec, None) is False

    def test_scratch_guard_name_is_pruned(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_dir(Path(), "node_modules", root_spec, None) is False

    def test_root_spec_match_prunes_the_name(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("build/\n")
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_dir(Path(), "build", root_spec, None) is False

    def test_local_spec_match_prunes_the_name(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("logs/\n")
        local_spec = rules.local_spec(sub)
        assert local_spec is not None
        assert rules.keeps_dir(Path("sub"), "logs", root_spec, local_spec) is False


class TestKeepsFile:
    def test_plain_name_is_kept(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_file(Path(), "app.py", root_spec, None) is True

    def test_root_spec_match_prunes_the_name(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_file(Path(), "debug.log", root_spec, None) is False

    def test_local_spec_match_prunes_the_name(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("*.tmp\n")
        local_spec = rules.local_spec(sub)
        assert local_spec is not None
        assert rules.keeps_file(Path("sub"), "scratch.tmp", root_spec, local_spec) is (
            False
        )

    def test_hidden_name_is_kept_unlike_keeps_dir(self, tmp_path: Path):
        """Unlike keeps_dir, keeps_file applies no hidden-name rule of its
        own -- that decision is left to each caller (module docstring).
        """
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_file(Path(), ".env", root_spec, None) is True

    def test_scratch_guard_name_is_kept_unlike_keeps_dir(self, tmp_path: Path):
        """keeps_file applies no ScratchGuard skip-name rule either -- that
        rule only prunes DIRECTORIES a walk would otherwise descend into.
        """
        rules = IgnoreRules(tmp_path, ScratchGuard())
        root_spec = rules.root_spec()
        assert rules.keeps_file(Path(), "node_modules", root_spec, None) is True


class TestExcludes:
    def test_none_spec_never_excludes(self) -> None:
        assert IgnoreRules.excludes(None, "anything", is_dir=False) is False
        assert IgnoreRules.excludes(None, "anything", is_dir=True) is False

    def test_directory_match_needs_the_trailing_slash_convention(self, tmp_path: Path):
        """A gitignore ``build/`` pattern only matches when is_dir=True appends
        the trailing slash -- the same name passed as a file never matches.
        """
        (tmp_path / ".gitignore").write_text("build/\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()
        assert IgnoreRules.excludes(spec, "build", is_dir=True) is True
        assert IgnoreRules.excludes(spec, "build", is_dir=False) is False

    def test_glob_pattern_matches_either_way(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        spec = IgnoreRules(tmp_path, ScratchGuard()).root_spec()
        assert IgnoreRules.excludes(spec, "debug.log", is_dir=False) is True
        assert IgnoreRules.excludes(spec, "debug.log", is_dir=True) is True


class TestReadIgnoreLines:
    def test_absent_file_returns_empty(self, tmp_path: Path):
        assert IgnoreRules._read_ignore_lines(tmp_path / ".gitignore") == []

    def test_unreadable_file_returns_empty_not_raise(self, tmp_path: Path):
        """A directory named .gitignore triggers IsADirectoryError, not a crash."""
        (tmp_path / ".gitignore").mkdir()
        assert IgnoreRules._read_ignore_lines(tmp_path / ".gitignore") == []

    def test_reads_lines_from_a_real_file(self, tmp_path: Path):
        path = tmp_path / ".gitignore"
        path.write_text("*.log\ndata/\n")
        assert IgnoreRules._read_ignore_lines(path) == ["*.log", "data/"]


def test_default_patterns_are_globs_not_dir_names():
    # Glob patterns stay in the pathspec defaults; scratch/VCS/cache DIR names
    # live in ScratchGuard (pruned by name), so they are not duplicated here.
    assert "*.pyc" in _DEFAULT_IGNORE_PATTERNS
    assert ".DS_Store" in _DEFAULT_IGNORE_PATTERNS
    assert "node_modules/" not in _DEFAULT_IGNORE_PATTERNS
    assert "venv/" not in _DEFAULT_IGNORE_PATTERNS
