"""Tests for the `quarry skills` command group (SkillsCli).

Exercised in isolation with a stub CliPlumbing (no daemon client -- deposit is
pure filesystem), the same pattern as ``test_cli_sync.py``/
``test_cli_captures.py``. ``Path.home`` is monkeypatched process-wide for the
duration of each test so the installer targets a throwaway directory instead
of the real machine. The CLI deposits from quarry's OWN packaged tree
(PL-1049), so these tests no longer build a fake ``plugin/skills/`` fixture --
they assert on the real ``quarry-recall``/``quarry-capture`` skills and run
from an arbitrary (not even quarry-shaped) CWD, proving the command works
anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import typer
from typer.testing import CliRunner

from quarry.cli_captures import CliPlumbing
from quarry.cli_skills import SkillsCli

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from quarry.client import QuarryClient

runner = CliRunner()

# Each emitted call recorded as (structured_data, human_text).
Emitted = tuple[object, str]

_PACKAGED_SKILLS = {"quarry-recall", "quarry-capture"}


def _plumbing(calls: list[Emitted], err_console: MagicMock) -> CliPlumbing:
    def emit(data: object, text: str = "") -> None:
        calls.append((data, text))

    def cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
        return fn

    return CliPlumbing(
        emit=emit,
        cli_errors=cli_errors,
        client=lambda: cast("QuarryClient", MagicMock()),
        err_console=err_console,
        is_quiet=lambda: False,
    )


def _app(calls: list[Emitted], err_console: MagicMock | None = None) -> typer.Typer:
    app = typer.Typer()
    console = err_console if err_console is not None else MagicMock()
    app.add_typer(SkillsCli(_plumbing(calls, console)).build(), name="skills")
    return app


def _rows(calls: list[Emitted]) -> list[dict[str, object]]:
    """Return the structured payload of the last ``emit`` call."""
    data = calls[-1][0]
    assert isinstance(data, list)
    return data


def _patch_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))


def _unrelated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Chdir into an ordinary, non-quarry directory -- no ``plugin/skills/``,
    no ``pyproject.toml`` at all -- proving the command deposits from the
    packaged tree regardless of where it is invoked from."""
    cwd = tmp_path / "some-unrelated-directory"
    cwd.mkdir()
    monkeypatch.chdir(cwd)


class TestInstall:
    def test_all_deposits_into_every_detected_harness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)
        calls: list[Emitted] = []

        result = runner.invoke(_app(calls), ["skills", "install", "--all"])

        assert result.exit_code == 0, result.output
        rows = _rows(calls)
        assert {r["harness"] for r in rows} == {"codex"}
        assert {r["skill"] for r in rows} >= _PACKAGED_SKILLS
        assert all(r["action"] == "deposited" for r in rows)
        assert (home / ".codex" / "skills" / "quarry-recall" / "SKILL.md").is_file()

    def test_default_with_no_flags_behaves_like_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".pi").mkdir(parents=True)
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)

        result = runner.invoke(_app([]), ["skills", "install"])

        assert result.exit_code == 0, result.output
        assert (home / ".pi" / "agent" / "skills" / "quarry-recall").is_dir()

    def test_agent_targets_one_harness_even_if_undetected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"  # no markers at all -- codex is undetected
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)

        result = runner.invoke(_app([]), ["skills", "install", "--agent", "codex"])

        assert result.exit_code == 0, result.output
        assert (home / ".codex" / "skills" / "quarry-recall").is_dir()

    def test_rerun_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)
        calls: list[Emitted] = []
        runner.invoke(_app(calls), ["skills", "install", "--agent", "codex"])

        result = runner.invoke(_app(calls), ["skills", "install", "--agent", "codex"])

        assert result.exit_code == 0, result.output
        assert all(r["action"] == "current" for r in _rows(calls))

    def test_unknown_agent_is_a_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, tmp_path / "home")
        err_console = MagicMock()

        result = runner.invoke(
            _app([], err_console), ["skills", "install", "--agent", "bogus"]
        )

        assert result.exit_code == 2
        message = err_console.print.call_args[0][0]
        assert "bogus" in message

    def test_agent_and_all_together_is_a_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, tmp_path / "home")

        result = runner.invoke(
            _app([]), ["skills", "install", "--agent", "codex", "--all"]
        )

        assert result.exit_code == 2

    def test_no_detected_harness_reports_plainly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, tmp_path / "home")  # no harness markers at all
        calls: list[Emitted] = []

        result = runner.invoke(_app(calls), ["skills", "install", "--all"])

        assert result.exit_code == 0
        assert _rows(calls) == []
        assert calls[-1][1] == "No harnesses detected."

    def test_all_deposits_into_multiple_detected_harnesses_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".pi").mkdir(parents=True)
        (home / ".config" / "opencode").mkdir(parents=True)
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)
        calls: list[Emitted] = []

        result = runner.invoke(_app(calls), ["skills", "install", "--all"])

        assert result.exit_code == 0, result.output
        rows = _rows(calls)
        assert {r["harness"] for r in rows} == {"pi", "opencode"}
        assert all(r["action"] == "deposited" for r in rows)
        assert (home / ".pi" / "agent" / "skills" / "quarry-recall").is_dir()
        assert (home / ".config" / "opencode" / "skills" / "quarry-recall").is_dir()

    def test_agent_claude_alongside_all_shows_the_unsupported_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)
        calls: list[Emitted] = []

        result = runner.invoke(_app(calls), ["skills", "install", "--agent", "claude"])

        assert result.exit_code == 0, result.output
        rows = _rows(calls)
        assert {r["skill"] for r in rows} >= _PACKAGED_SKILLS
        assert all(r["action"] == "unsupported" and r["path"] is None for r in rows)


class TestStatus:
    def test_reports_absent_then_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _unrelated_cwd(tmp_path, monkeypatch)
        _patch_home(monkeypatch, home)
        calls: list[Emitted] = []

        runner.invoke(_app(calls), ["skills", "status"])
        assert all(r["action"] in {"absent", "unsupported"} for r in _rows(calls))

        runner.invoke(_app(calls), ["skills", "install", "--agent", "codex"])
        runner.invoke(_app(calls), ["skills", "status"])
        codex_rows = [r for r in _rows(calls) if r["harness"] == "codex"]
        assert codex_rows
        assert all(r["action"] == "current" for r in codex_rows)
