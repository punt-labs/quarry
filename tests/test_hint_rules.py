"""Tests for convention hint rules."""

from __future__ import annotations

from quarry.hint_accumulator import ToolEvent
from quarry.hint_rules import (
    check_instant_rules,
    check_sequence_rules,
)

# ---------------------------------------------------------------------------
# Instant rules
# ---------------------------------------------------------------------------


class TestGitAddRule:
    def test_git_add_dash_a(self) -> None:
        assert check_instant_rules("git add -A") is not None

    def test_git_add_dot(self) -> None:
        assert check_instant_rules("git add .") is not None

    def test_git_add_specific_file(self) -> None:
        assert check_instant_rules("git add src/main.py") is None

    def test_git_add_multiple_files(self) -> None:
        assert check_instant_rules("git add foo.py bar.py") is None


class TestPipInstallRule:
    def test_pip_install(self) -> None:
        assert check_instant_rules("pip install requests") is not None

    def test_uv_pip_install_no_trigger(self) -> None:
        assert check_instant_rules("uv pip install requests") is None

    def test_uv_add(self) -> None:
        assert check_instant_rules("uv add requests") is None


class TestGitAddVariants:
    def test_git_add_dash_a_with_path(self) -> None:
        """git add -A src/ is still a broad stage."""
        assert check_instant_rules("git add -A src/") is not None

    def test_git_add_dot_with_flag(self) -> None:
        assert check_instant_rules("git add . --update") is not None


class TestForcePushRule:
    def test_force_push_long_flag(self) -> None:
        assert check_instant_rules("git push --force origin main") is not None

    def test_force_push_short_flag(self) -> None:
        assert check_instant_rules("git push -f origin main") is not None

    def test_force_with_lease_no_trigger(self) -> None:
        """--force-with-lease is the safe alternative — no hint."""
        assert check_instant_rules("git push --force-with-lease origin main") is None

    def test_regular_push(self) -> None:
        assert check_instant_rules("git push origin main") is None

    def test_push_with_set_upstream(self) -> None:
        assert check_instant_rules("git push -u origin feat/x") is None


class TestNoVerifyRule:
    def test_no_verify_long(self) -> None:
        hint = check_instant_rules('git commit --no-verify -m "skip"')
        assert hint is not None

    def test_no_verify_short(self) -> None:
        hint = check_instant_rules('git commit -n -m "skip"')
        assert hint is not None

    def test_normal_commit(self) -> None:
        assert check_instant_rules('git commit -m "feat: add thing"') is None

    def test_no_verify_after_message(self) -> None:
        """--no-verify after -m 'message' must still trigger."""
        hint = check_instant_rules('git commit -m "fix" --no-verify')
        assert hint is not None

    def test_short_n_after_message(self) -> None:
        """-n after -m 'message' must still trigger."""
        hint = check_instant_rules('git commit -m "fix" -n')
        assert hint is not None

    def test_no_false_positive_on_message_with_n(self) -> None:
        """'-n' in commit message should not trigger."""
        assert check_instant_rules('git commit -m "fix -n edge"') is None

    def test_chained_head_n_no_false_positive(self) -> None:
        """head -n in a chained command must not trigger no-verify hint."""
        assert check_instant_rules('head -n 5 file && git commit -m "fix"') is None

    def test_chained_tail_n_no_false_positive(self) -> None:
        """tail -n in a chained command must not trigger no-verify hint."""
        assert check_instant_rules('tail -n 10 log && git commit -m "fix"') is None


class TestNonMatchingCommands:
    def test_ls(self) -> None:
        assert check_instant_rules("ls -la") is None

    def test_uv_run(self) -> None:
        assert check_instant_rules("uv run pytest tests/ -v") is None

    def test_empty_string(self) -> None:
        assert check_instant_rules("") is None


# ---------------------------------------------------------------------------
# Sequence rules
# ---------------------------------------------------------------------------


def _event(command: str, ts: float = 100.0) -> ToolEvent:
    return ToolEvent(ts=ts, tool="Bash", command=command)


class TestCommitWithoutGateRule:
    def test_commit_without_gate_triggers(self) -> None:
        events = [_event("uv run mypy src/"), _event("uv run ruff check .")]
        hint = check_sequence_rules(events, 'git commit -m "fix"')
        assert hint is not None
        assert "quality gate" in hint

    def test_commit_after_full_gate_no_trigger(self) -> None:
        full = (
            "uv run ruff check . && uv run ruff format --check . "
            "&& uv run mypy src/ tests/ && uv run pyright && uv run pytest"
        )
        events = [_event(full)]
        hint = check_sequence_rules(events, 'git commit -m "feat"')
        assert hint is None

    def test_commit_after_make_check_no_trigger(self) -> None:
        # make check expands to a command containing all components
        gate = (
            "ruff check . && ruff format --check . && mypy src/ tests/ "
            "&& pyright && pytest"
        )
        events = [_event(gate)]
        hint = check_sequence_rules(events, 'git commit -m "feat"')
        assert hint is None

    def test_non_commit_command_no_trigger(self) -> None:
        events: list[ToolEvent] = []
        assert check_sequence_rules(events, "git push origin main") is None


class TestSoloGateToolRule:
    def test_second_solo_tool_triggers(self) -> None:
        events = [_event("uv run mypy src/")]
        hint = check_sequence_rules(events, "uv run ruff check .")
        assert hint is not None
        assert "full quality gate" in hint

    def test_first_solo_tool_no_trigger(self) -> None:
        events: list[ToolEvent] = []
        hint = check_sequence_rules(events, "uv run mypy src/")
        assert hint is None

    def test_non_gate_tool_no_trigger(self) -> None:
        events = [_event("uv run mypy src/")]
        hint = check_sequence_rules(events, "ls -la")
        assert hint is None

    def test_full_chain_not_flagged(self) -> None:
        """A chained command is not a solo gate tool."""
        events = [_event("uv run mypy src/")]
        full = "uv run ruff check . && uv run pytest"
        hint = check_sequence_rules(events, full)
        assert hint is None

    def test_chained_past_event_not_counted(self) -> None:
        """Past chained commands should not count as solo gate tools."""
        events = [_event("uv run ruff check . && uv run pytest")]
        hint = check_sequence_rules(events, "uv run mypy src/")
        assert hint is None
