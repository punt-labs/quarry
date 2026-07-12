"""Tests for the ported vox suppression ratchet (``tools/suppression``).

The suppression package is vox-verbatim except ``patterns.py``, which swaps
vox's regex+AST heuristic for a ``tokenize`` scan. These tests pin that swap
(the four documented blind spots must now count correctly, and string-interior
``noqa`` text must not), the per-file-ignores counter's fail-closed parsing, and
the merge-base count delta driven end-to-end through a real git tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import tools.suppression
from tools.suppression import (
    FileSuppressions,
    GitError,
    GitRepo,
    PerFileIgnoresCounter,
    SuppressionReport,
)
from tools.suppression.pyproject import PyprojectError

if TYPE_CHECKING:
    from tests.conftest import GitSandbox


def test_module_is_importable_as_package() -> None:
    """The package resolves as ``tools.suppression`` (regular-package path)."""
    assert "FileSuppressions" in tools.suppression.__all__


class TestTokenizeScan:
    """The tokenize scan counts real suppressions and ignores string interiors."""

    def test_noqa_on_code_line_counted(self) -> None:
        fs = FileSuppressions("f.py", "x = 1  # noqa: E501\n")
        assert fs.count("noqa") == 1

    def test_type_ignore_counted(self) -> None:
        fs = FileSuppressions("f.py", "import x  # type: ignore[import-not-found]\n")
        assert fs.count("type_ignore") == 1

    def test_pyright_ignore_counted(self) -> None:
        fs = FileSuppressions("f.py", "y = z()  # pyright: ignore[reportUnknown]\n")
        assert fs.count("pyright_ignore") == 1

    def test_pylint_disable_counted(self) -> None:
        fs = FileSuppressions("f.py", "x = 1  # pylint: disable=invalid-name\n")
        assert fs.count("pylint_disable") == 1

    def test_pure_comment_line_not_counted(self) -> None:
        fs = FileSuppressions("f.py", "# noqa: E501 standalone comment\nx = 1\n")
        assert fs.count("noqa") == 0

    def test_noqa_inside_multiline_string_not_counted(self) -> None:
        source = '"""line one\n# noqa: E501 inside a string\n"""\nx = 1\n'
        fs = FileSuppressions("f.py", source)
        assert fs.count("noqa") == 0

    def test_untokenizable_source_fails_closed_not_zeroed(self) -> None:
        """Untokenizable source must fail CLOSED (over-count), never zero out.

        Zeroing would let the suppression gate read a PR that both broke
        tokenization and added suppressions as "count decreased -> PASS". The
        fallback over-counts marker-bearing lines instead, so the marker on the
        code line is still seen and the total can only rise or hold.
        """
        fs = FileSuppressions("f.py", '"""open string never closes\nx = 1  # noqa\n')
        assert fs.count("noqa") >= 1
        assert fs.total >= 1

    # --- the four documented blind spots of the retired regex+AST heuristic ---

    def test_blindspot_async_def_counted(self) -> None:
        """``async def`` was missing from the old regex keyword list."""
        fs = FileSuppressions("f.py", "async def f():  # noqa: D103\n    return 1\n")
        assert fs.count("noqa") == 1

    def test_blindspot_attribute_assignment_counted(self) -> None:
        """``obj.attr =`` starts with identifier+dot, which the regex missed."""
        fs = FileSuppressions("f.py", "c.x = 1  # noqa: D101\n")
        assert fs.count("noqa") == 1

    def test_blindspot_tuple_target_counted(self) -> None:
        """``a, b = ...`` starts with identifier+comma, which the regex missed."""
        fs = FileSuppressions("f.py", "a, b = 1, 2  # noqa: E501\n")
        assert fs.count("noqa") == 1

    def test_blindspot_single_line_docstring_with_noqa_not_counted(self) -> None:
        """A one-line docstring containing ``noqa`` is string content, not code.

        The retired heuristic treated a line starting with a triple quote as
        code and would have counted this false positive.
        """
        fs = FileSuppressions("f.py", '"""docs # noqa: E501"""\nx = 1\n')
        assert fs.count("noqa") == 0

    def test_comment_after_closing_docstring_quote_counted(self) -> None:
        """A real comment after the closing quote on the same line IS counted."""
        fs = FileSuppressions("f.py", '"""docs"""  # noqa: E501\nx = 1\n')
        assert fs.count("noqa") == 1


class TestPerFileIgnoresCounter:
    """Fail-closed parsing of ``[tool.ruff.lint.per-file-ignores]``."""

    def test_missing_pyproject_is_zero(self, tmp_path: Path) -> None:
        counter = PerFileIgnoresCounter(tmp_path / "nope.toml")
        assert counter.total == 0
        assert counter.breakdown == {}

    def test_well_formed_counts(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.ruff.lint.per-file-ignores]\n"
            '"tests/*" = ["S101", "PLR2004"]\n'
            '"docs/*" = ["E501"]\n'
        )
        counter = PerFileIgnoresCounter(pyproject)
        assert counter.total == 3
        assert counter.breakdown == {"tests/*": 2, "docs/*": 1}

    def test_without_section_is_zero(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n')
        assert PerFileIgnoresCounter(pyproject).total == 0

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        """Silent miscounts would let suppressions slip past the ratchet."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.ruff.lint.per-file-ignores\n")  # missing ]
        with pytest.raises(PyprojectError, match="invalid TOML"):
            PerFileIgnoresCounter(pyproject)

    def test_unreadable_existing_pyproject_raises(self, tmp_path: Path) -> None:
        """A present-but-unreadable pyproject must surface the failure."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n')
        with (
            patch.object(Path, "read_text", side_effect=OSError("permission denied")),
            pytest.raises(PyprojectError, match="cannot read"),
        ):
            PerFileIgnoresCounter(pyproject)


class TestSuppressionReport:
    """Aggregation across files plus the per-file-ignores config count."""

    def test_totals_across_files_and_config(self) -> None:
        a = FileSuppressions("a.py", "x = 1  # noqa\n")
        b = FileSuppressions("b.py", "y = 2  # type: ignore\n")
        report = SuppressionReport([a, b], per_file_ignores_count=3)
        assert report.total == 5
        assert report.by_category["noqa"] == 1
        assert report.by_category["type_ignore"] == 1
        assert report.by_category["per_file_ignores"] == 3


_SRC = "x = 1  # noqa: E501\n"


def _seat_and_commit(sandbox: GitSandbox, source: str) -> str:
    """Write src/mod.py, seat the suppression baseline, and commit; return base."""
    sandbox.write("src/mod.py", source)
    assert tools.suppression.main(["src", "--update", "--allow-ci-write"]) == 0
    return sandbox.commit("base")


class TestMergeBaseCheck:
    """--check compares the current count against the base-commit baseline."""

    def test_unchanged_passes(
        self, git_sandbox: GitSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        base = _seat_and_commit(git_sandbox, _SRC)
        code = tools.suppression.main(
            ["src", "--check", "--base-ref", base, "--require-base"]
        )
        assert code == 0

    def test_increase_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        base = _seat_and_commit(git_sandbox, _SRC)
        # Add one more suppression on a code line — the count rises above base.
        git_sandbox.write("src/mod.py", _SRC + "y = 2  # type: ignore\n")
        code = tools.suppression.main(
            ["src", "--check", "--base-ref", base, "--require-base"]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "increased by 1" in out

    def test_decrease_passes(
        self, git_sandbox: GitSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        base = _seat_and_commit(git_sandbox, _SRC + "y = 2  # type: ignore\n")
        git_sandbox.write("src/mod.py", _SRC)  # one fewer suppression
        code = tools.suppression.main(
            ["src", "--check", "--base-ref", base, "--require-base"]
        )
        assert code == 0

    def test_untokenizable_edit_cannot_launder_increase(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A PR that adds a suppression AND breaks tokenization must FAIL.

        The base baseline records the file's count while it tokenized cleanly;
        the base side is never re-tokenized. If the worktree re-scan zeroed the
        now-untokenizable file, its total would drop below base and the gate
        would read "count decreased -> PASS", hiding the old and newly added
        suppressions. The fail-closed over-count keeps the total above base.
        """
        monkeypatch.chdir(git_sandbox.root)
        base = _seat_and_commit(git_sandbox, _SRC)  # 1 suppression, tokenizes
        # Add a suppression AND make the file untokenizable (unterminated string).
        git_sandbox.write(
            "src/mod.py", _SRC + 'y = 2  # type: ignore\nz = """unterminated\n'
        )
        code = tools.suppression.main(
            ["src", "--check", "--base-ref", base, "--require-base"]
        )
        out = capsys.readouterr().out
        assert code == 1  # NOT "count decreased -> PASS"
        assert "increased" in out


class TestFailClosed:
    """A corrupt base baseline blob fails closed rather than fail-open."""

    def test_non_dict_blob_raises_git_error(self, git_sandbox: GitSandbox) -> None:
        git_sandbox.write("src/mod.py", _SRC)
        git_sandbox.write(".suppression-baseline.json", "[]\n")
        sha = git_sandbox.commit("corrupt baseline")
        git = GitRepo(git_sandbox.root)
        with pytest.raises(GitError, match="non-dict"):
            git.show_baseline(sha)

    def test_check_reports_nonzero_on_corrupt_blob(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _SRC)
        git_sandbox.write(".suppression-baseline.json", "[]\n")
        sha = git_sandbox.commit("corrupt baseline")
        code = tools.suppression.main(
            ["src", "--check", "--base-ref", sha, "--require-base"]
        )
        assert code == 1
        assert "FAIL" in capsys.readouterr().out
