"""Tests for the ported vox coupling ratchet (``tools/coupling``).

The coupling gate is regression-only: a touched file fails only if a metric is
worse than its base-commit baseline; holding steady or adding a clean new file
passes. ``__main__.py`` gets a relaxed threshold table. These tests pin the
threshold semantics (including the MAIN_TABLE relaxation) and drive the
regression-only verdict end-to-end through a real git tree.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import tools.coupling
from tools.coupling import CouplingScorer, CouplingThresholds, GitError, GitRepo

if TYPE_CHECKING:
    from tests.conftest import GitSandbox


def test_module_is_importable_as_package() -> None:
    """The package resolves as ``tools.coupling`` (regular-package path)."""
    assert "CouplingRatchet" in tools.coupling.__all__


class TestThresholds:
    """Strict table for most files; relaxed MAIN_TABLE for ``__main__.py``."""

    def test_efferent_strict_for_regular_module(self) -> None:
        assert CouplingThresholds.meets("efferent_coupling", 7, "src/pkg/core.py")
        assert not CouplingThresholds.meets("efferent_coupling", 8, "src/pkg/core.py")

    def test_efferent_relaxed_for_main(self) -> None:
        # __main__.py legitimately wires many modules — relaxed to <= 15.
        assert CouplingThresholds.meets("efferent_coupling", 12, "src/pkg/__main__.py")
        assert not CouplingThresholds.meets(
            "efferent_coupling", 16, "src/pkg/__main__.py"
        )

    def test_public_names_relaxed_for_main(self) -> None:
        assert CouplingThresholds.meets("public_names", 90, "src/pkg/__main__.py")
        assert not CouplingThresholds.meets("public_names", 90, "src/pkg/core.py")

    def test_better_or_equal_and_strictly_better(self) -> None:
        assert CouplingThresholds.better_or_equal("efferent_coupling", 3, 3)
        assert not CouplingThresholds.better_or_equal("efferent_coupling", 4, 3)
        assert CouplingThresholds.strictly_better("efferent_coupling", 2, 3)
        assert not CouplingThresholds.strictly_better("efferent_coupling", 3, 3)


def _seat_package(sandbox: GitSandbox, hub_imports: str) -> None:
    """Write a 4-module package with hub importing ``hub_imports`` and seat it."""
    sandbox.write("src/pkg/a.py", "x = 1\n")
    sandbox.write("src/pkg/b.py", "x = 2\n")
    sandbox.write("src/pkg/c.py", "x = 3\n")
    sandbox.write("src/pkg/hub.py", hub_imports)
    assert tools.coupling.main(["src/pkg", "--rebaseline", "--allow-ci-write"]) == 0


class TestRegressionOnlyVerdict:
    """A touched file fails only on regression; new/steady files pass."""

    def test_efferent_regression_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        _seat_package(git_sandbox, "from . import a\n")  # efferent 1
        base = git_sandbox.commit("base")
        # hub now imports three siblings — efferent 1 -> 3 regresses.
        git_sandbox.write("src/pkg/hub.py", "from . import a, b, c\n")
        assert tools.coupling.main(["src/pkg", "--rebaseline", "--allow-ci-write"]) == 0
        code = tools.coupling.main(
            ["src/pkg", "--check", "--base-ref", base, "--require-base"]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "regression detected" in out
        assert "efferent_coupling" in out

    def test_new_file_under_threshold_passes(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        _seat_package(git_sandbox, "from . import a\n")
        base = git_sandbox.commit("base")
        # A brand-new module with efferent 1 is INFO, never a regression. It must
        # be committed to appear in `git diff <base>` (untracked files do not).
        git_sandbox.write("src/pkg/d.py", "from . import a\n")
        assert tools.coupling.main(["src/pkg", "--rebaseline", "--allow-ci-write"]) == 0
        git_sandbox.commit("add d")
        code = tools.coupling.main(
            ["src/pkg", "--check", "--base-ref", base, "--require-base"]
        )
        assert code == 0
        assert "no regressions" in capsys.readouterr().out

    def test_unresolvable_base_fails_closed(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        _seat_package(git_sandbox, "from . import a\n")
        git_sandbox.commit("base")
        code = tools.coupling.main(
            ["src/pkg", "--check", "--base-ref", "nope", "--require-base"]
        )
        assert code == 1
        assert "unresolvable" in capsys.readouterr().out


class TestBaselineBootstrap:
    """The bootstrap verb must populate the baseline, never leave it empty.

    ``--update`` is incremental (it banks improvements against an existing
    baseline) so on a fresh tree it writes ``{}``. An empty base baseline then
    trips ``_empty_baseline`` under ``--require-base`` on every future PR. The
    full-tree ``--rebaseline`` verb writes real per-file metrics.
    """

    def test_rebaseline_populates_not_empty(
        self, git_sandbox: GitSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        _seat_package(git_sandbox, "from . import a\n")  # uses --rebaseline
        data = json.loads((git_sandbox.root / ".oo-coupling-baseline.json").read_text())
        assert data  # non-empty — the bug produced {}
        assert "src/pkg/hub.py" in data
        assert data["src/pkg/hub.py"]["efferent_coupling"] == 1.0

    def test_populated_base_unchanged_passes(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A populated base baseline + unchanged coupling passes the gate."""
        monkeypatch.chdir(git_sandbox.root)
        _seat_package(git_sandbox, "from . import a\n")
        base = git_sandbox.commit("base")  # populated baseline committed
        # Touch hub.py without changing any coupling metric.
        git_sandbox.write("src/pkg/hub.py", "from . import a\n# note: unchanged\n")
        code = tools.coupling.main(
            ["src/pkg", "--check", "--base-ref", base, "--require-base"]
        )
        assert code == 0
        assert "no regressions" in capsys.readouterr().out


class TestPushTripwireAdoption:
    """The push:[main] tripwire bootstrap-passes the one-time adoption merge.

    Base ``HEAD~1`` predates the newly added coupling baseline; the merged tip
    carries it. On the PR path that reads as a stale branch → "rebase onto
    current main" (fail-closed, kept). On the push tripwire there is no branch
    to rebase — it IS main — so it bootstrap-passes.
    """

    def _adoption_repo(self, sandbox: GitSandbox) -> str:
        """Build a pre-adoption commit (no baseline) then the adoption commit.

        Points ``refs/remotes/origin/main`` at the adoption tip so the PR-path
        ``_absent_base_baseline`` sees a tip that carries the baseline. Returns
        the pre-adoption sha (base with no coupling baseline).
        """
        sandbox.write("src/pkg/a.py", "x = 1\n")
        sandbox.write("src/pkg/hub.py", "from . import a\n")
        pre = sandbox.commit("pre-adoption (no coupling baseline)")
        assert tools.coupling.main(["src/pkg", "--rebaseline", "--allow-ci-write"]) == 0
        adoption = sandbox.commit("adoption (adds coupling baseline)")
        sandbox.run_git("update-ref", "refs/remotes/origin/main", adoption)
        return pre

    def test_pr_path_fails_rebase(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        pre = self._adoption_repo(git_sandbox)
        code = tools.coupling.main(
            ["src/pkg", "--check", "--base-ref", pre, "--require-base"]
        )
        assert code == 1
        assert "rebase onto current main" in capsys.readouterr().out

    def test_push_tripwire_bootstrap_passes(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        pre = self._adoption_repo(git_sandbox)
        code = tools.coupling.main(
            ["src/pkg", "--check", "--base-ref", pre, "--require-base", "--tripwire"]
        )
        assert code == 0
        assert "bootstrap pass" in capsys.readouterr().out


class TestFailClosed:
    """A corrupt base coupling baseline blob fails closed, not open."""

    def test_non_dict_blob_raises_git_error(self, git_sandbox: GitSandbox) -> None:
        git_sandbox.write("src/pkg/a.py", "x = 1\n")
        git_sandbox.write(".oo-coupling-baseline.json", "[]\n")
        sha = git_sandbox.commit("corrupt baseline")
        git = GitRepo(git_sandbox.root)
        with pytest.raises(GitError, match="non-dict"):
            git.show_baseline(sha)


class TestScorerKeys:
    """Scorer keys are repo-relative POSIX so baseline and diff paths intersect."""

    def test_keys_repo_relative(
        self, git_sandbox: GitSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/pkg/a.py", "x = 1\n")
        git_sandbox.write("src/pkg/hub.py", "from . import a\n")
        scorer = CouplingScorer(git_sandbox.root / "src/pkg", git_sandbox.root)
        assert "src/pkg/hub.py" in scorer.files
