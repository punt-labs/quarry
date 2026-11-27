"""Tests for the enable/disable CLI summary presenters."""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

from quarry.claude_permissions import PermissionChange
from quarry.db_pointer import SELECTION
from quarry.disable_report import DisableReport
from quarry.enable import DisableResult, EnableResult
from quarry.enable_report import EnableReport
from quarry.ethos_memory import EthosMemoryResult
from quarry.skills_install import Harness, SkillOutcome

pytestmark = pytest.mark.usefixtures("confined_config_reads")


def _enable(p: str, ethos: EthosMemoryResult) -> EnableResult:
    """Return a minimal EnableResult carrying *ethos* as its bootstrap outcome."""
    return EnableResult(
        directory=p, collection="p", captures_collection="p-captures", ethos=ethos
    )


def test_enable_report_lists_claudemd_steps(p: str) -> None:
    result = EnableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        config_path=f"{p}/.punt-labs/quarry/config.md",
        guide_deposited=True,
        enabled_marker_written=True,
        import_registered=True,
        gitignore_ensured=True,
    )
    lines = EnableReport(result).lines()
    assert lines[0] == f"Enabled quarry for {p}"
    joined = "\n".join(lines)
    assert "Deposited quarry guide" in joined
    assert "Registered @.punt-labs/quarry/CLAUDE.md" in joined
    # Symmetry with DisableReport's marker line: the write is surfaced.
    assert "Wrote enabled marker" in joined
    assert ".gitignore excludes captures and lock files" in joined


def test_enable_report_omits_absent_steps(p: str) -> None:
    result = EnableResult(directory=p, collection="p", captures_collection="p-captures")
    joined = "\n".join(EnableReport(result).lines())
    assert "Registered @" not in joined
    # An idempotent re-enable's marker no-op is visible by its absence, like
    # DisableReport omits the marker line when nothing was removed.
    assert "Wrote enabled marker" not in joined
    assert ".gitignore excludes captures and lock files" not in joined
    assert "Ethos" not in joined


def test_enable_report_gitignore_message_states_postcondition_not_delta(p: str) -> None:
    """Report the .gitignore postcondition rather than a per-entry delta.

    gitignore_ensured covers both captures and FileLock artifacts. The message
    must be accurate whether this run adds one exclusion or both.
    """
    result = EnableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        gitignore_ensured=True,
    )
    joined = "\n".join(EnableReport(result).lines())
    assert ".gitignore excludes captures and lock files" in joined
    # The old wording claimed one specific entry -- must not reappear.
    assert "Added captures/ exclusion" not in joined


def test_enable_report_reports_ethos_skipped(p: str) -> None:
    report = EnableReport(_enable(p, EthosMemoryResult(skipped=True)))
    assert "  Ethos: not installed (agent memory skipped)" in report.lines()


def test_enable_report_reports_created_and_memory_collections(p: str) -> None:
    joined = "\n".join(
        EnableReport(_enable(p, EthosMemoryResult(created=["rmh"]))).lines()
    )
    assert "Ethos created: rmh" in joined
    assert "Memory collections: memory-rmh" in joined


def test_enable_report_announces_vendored_refresh_as_a_diff_to_commit(p: str) -> None:
    result = _enable(p, EthosMemoryResult(vendored_updated=["claude", "rmh"]))
    joined = "\n".join(EnableReport(result).lines())
    assert "Ethos guide refreshed (vendored — commit via PR): claude, rmh" in joined


def test_enable_report_vendored_line_accompanies_not_installed(p: str) -> None:
    """A repo can carry a vendored tree while the operator has no global ethos."""
    result = _enable(p, EthosMemoryResult(skipped=True, vendored_updated=["rmh"]))
    joined = "\n".join(EnableReport(result).lines())
    assert "Ethos: not installed (agent memory skipped)" in joined
    assert "Ethos guide refreshed (vendored — commit via PR): rmh" in joined


def test_enable_report_omits_vendored_line_when_nothing_refreshed(p: str) -> None:
    joined = "\n".join(EnableReport(_enable(p, EthosMemoryResult())).lines())
    assert "vendored" not in joined


def test_enable_report_reports_ethos_failed_last(p: str) -> None:
    result = _enable(p, EthosMemoryResult(created=["rmh"], failed=["rmh"]))
    assert EnableReport(result).lines()[-1] == "  Ethos FAILED: rmh"


def test_disable_report_purge_queued_when_not_keep_data(p: str) -> None:
    result = DisableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        removed=3,
        import_pruned=True,
        enabled_marker_removed=True,
    )
    joined = "\n".join(DisableReport(result, keep_data=False).lines())
    assert "Deregistered p (3 files); chunk purge queued" in joined
    assert "Removed @.punt-labs/quarry/CLAUDE.md" in joined
    assert "Removed enabled marker (guide left dormant)" in joined


def test_disable_report_keeps_data_when_flag_set(p: str) -> None:
    result = DisableResult(
        directory=p, collection="p", captures_collection="p-captures", removed=3
    )
    joined = "\n".join(DisableReport(result, keep_data=True).lines())
    assert "kept indexed data" in joined


def test_disable_report_idempotent_noop(p: str) -> None:
    result = DisableResult(directory=p, collection="", captures_collection="")
    joined = "\n".join(DisableReport(result, keep_data=False).lines())
    assert "Already disabled (no registration)" in joined


def _still_captured(p: str) -> str:
    return (
        f"{p}/src is still active through the marker at {p} "
        "(session-start sync runs there); web, transcript and other capture hooks "
        "follow their own auto_capture settings; "
        f"`quarry disable {shlex.quote(p)}` ends it but deregisters {p} and purges "
        f"everything indexed for {p} (add --keep-data to keep the data)"
    )


def test_disable_report_deregistered_output_is_exact(p: str) -> None:
    result = DisableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        removed=3,
        config_removed=True,
        import_pruned=True,
        enabled_marker_removed=True,
        permissions=PermissionChange(changed=("Bash(quarry:*)",)),
    )
    assert DisableReport(result, keep_data=False).lines() == [
        f"Disabled quarry for {p}",
        "  Deregistered p (3 files); chunk purge queued",
        "  Config file removed",
        "  Removed @.punt-labs/quarry/CLAUDE.md from CLAUDE.md",
        "  Removed enabled marker (guide left dormant)",
        (
            "  Removed quarry tool permissions from .claude/settings.json "
            "(this project only): Bash(quarry:*)"
        ),
    ]
    assert DisableReport(result, keep_data=True).lines()[1] == (
        "  Deregistered p (3 files); kept indexed data"
    )


def test_disable_report_unregistered_output_is_exact(p: str) -> None:
    result = DisableResult(directory=p, collection="", captures_collection="")
    assert DisableReport(result, keep_data=False).lines() == [
        f"Disabled quarry for {p}",
        "  Already disabled (no registration)",
    ]


def test_disable_report_covered_child_parent_capturing_names_parent_command(
    p: str,
) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=p,
        config_removed=True,
        enabled_marker_removed=True,
    )
    lines = DisableReport(result, keep_data=False).lines()
    assert lines == [
        f"Removed quarry's own settings from {p}/src",
        "  Config file removed",
        "  Removed enabled marker (guide left dormant)",
        _still_captured(p),
    ]


def test_disable_report_covered_child_parent_not_capturing_does_not_suggest_it(
    p: str,
) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        enabled_marker_removed=True,
    )
    lines = DisableReport(result, keep_data=False).lines()
    assert lines[-1] == (
        f"{p}/src is registered under {p} but session start does not activate "
        "quarry there: "
        f"no readable enabled marker from {p}/src up to {p}"
    )
    assert f"quarry disable {p}" not in "\n".join(lines)
    assert "still active" not in "\n".join(lines)


def test_disable_report_covered_child_nested_marker_names_it(p: str) -> None:
    result = DisableResult(
        directory=f"{p}/team/svc",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=f"{p}/team",
        enabled_marker_removed=True,
    )
    assert DisableReport(result, keep_data=False).lines()[-1] == (
        f"{p}/team/svc is still active through the marker at {p}/team "
        "(session-start sync runs there); web, transcript and other capture "
        "hooks follow their own auto_capture settings; "
        f"`quarry disable {p}/team` ends it, as does disabling {p}"
    )


def test_disable_report_covered_child_nothing_own_says_so(p: str) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=p,
    )
    assert DisableReport(result, keep_data=False).lines() == [
        f"Nothing of its own found in {p}/src",
        _still_captured(p),
    ]


def test_disable_report_covered_child_permission_error_is_nothing_removed(
    p: str,
) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=p,
        permissions=PermissionChange(error="bad json"),
    )
    expected = [
        f"Nothing of its own found in {p}/src",
        "  Permissions not changed: bad json",
        _still_captured(p),
    ]
    assert DisableReport(result, keep_data=False).lines() == expected
    assert DisableReport(result, keep_data=False).lines() == expected


def test_disable_report_covered_child_permission_change_is_a_removal(p: str) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=p,
        permissions=PermissionChange(changed=("Bash(quarry:*)",)),
    )
    assert DisableReport(result, keep_data=False).lines()[0] == (
        f"Removed quarry's own settings from {p}/src"
    )


def test_disable_report_captured_note_names_the_purge_and_keep_data(p: str) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=p,
    )
    note = DisableReport(result, keep_data=False).lines()[-1]
    assert f"purges everything indexed for {p}" in note
    assert "--keep-data" in note


def test_enable_report_lists_granted_rules_and_scope(p: str) -> None:
    result = EnableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        permissions=PermissionChange(changed=("mcp__quarry__*", "Skill(quarry:find)")),
    )
    joined = "\n".join(EnableReport(result).lines())
    assert (
        "  Added quarry permission rules to .claude/settings.json (this project only): "
        "mcp__quarry__*, Skill(quarry:find)"
    ) in joined


def test_enable_report_says_when_rules_already_present(p: str) -> None:
    lines = EnableReport(_enable(p, EthosMemoryResult())).lines()
    assert "  Quarry permission rules already in .claude/settings.json" in lines


def test_enable_report_surfaces_permission_error(p: str) -> None:
    result = EnableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        permissions=PermissionChange(error="bad json"),
    )
    assert "  Permissions not changed: bad json" in EnableReport(result).lines()


def test_enable_report_shows_skills_line_when_deposited(p: str) -> None:
    result = EnableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        skills_deposited=(
            SkillOutcome(Harness.PI, "quarry-recall", "deposited", Path(p)),
            SkillOutcome(Harness.CODEX, "quarry-recall", "upgraded", Path(f"{p}-c")),
            # a second skill on an already-seen harness must not repeat it
            SkillOutcome(Harness.PI, "quarry-capture", "deposited", Path(f"{p}2")),
        ),
    )
    joined = "\n".join(EnableReport(result).lines())
    assert "  Deposited agent skills to: pi, codex" in joined


def test_enable_report_omits_skills_line_when_nothing_new(p: str) -> None:
    """A re-enable where every skill is already current shows nothing new."""
    result = EnableResult(
        directory=p,
        collection="p",
        captures_collection="p-captures",
        skills_deposited=(
            SkillOutcome(Harness.PI, "quarry-recall", "current", Path(p)),
            SkillOutcome(Harness.CLAUDE, "quarry-recall", "unsupported", None),
        ),
    )
    joined = "\n".join(EnableReport(result).lines())
    assert "Deposited agent skills" not in joined


def test_enable_report_omits_skills_line_when_empty(p: str) -> None:
    """No source tree/no detected harness at all -- the common case."""
    joined = "\n".join(EnableReport(_enable(p, EthosMemoryResult())).lines())
    assert "Deposited agent skills" not in joined


def test_disable_report_lists_removed_rules_and_omits_noop(p: str) -> None:
    removed = DisableResult(
        directory=p,
        collection="",
        captures_collection="",
        permissions=PermissionChange(changed=("Skill(quarry:find)",)),
    )
    noop = DisableResult(directory=p, collection="", captures_collection="")
    assert (
        "  Removed quarry tool permissions from .claude/settings.json "
        "(this project only): Skill(quarry:find)"
    ) in DisableReport(removed, keep_data=False).lines()
    assert not any(
        "settings.json" in line for line in DisableReport(noop, keep_data=False).lines()
    )


@pytest.fixture
def db_override() -> Iterator[None]:
    """Leave the process-wide ``--db`` override cleared after a test sets it."""
    yield
    SELECTION.override("")


def _captured_command(parent: str, *, keep_data: bool = False) -> list[str]:
    """Return the argv of the parent-disable command the covered-child note offers."""
    result = DisableResult(
        directory=f"{parent}/src",
        collection="",
        captures_collection="",
        covered_by=parent,
        capturing_marker=parent,
    )
    note = DisableReport(result, keep_data=keep_data).lines()[-1]
    return shlex.split(note.split("`")[1])


def test_captured_note_command_is_plain_without_a_db_override(p: str) -> None:
    assert _captured_command(p) == ["quarry", "disable", p]


@pytest.mark.usefixtures("db_override")
def test_captured_note_command_carries_the_db_override(p: str) -> None:
    SELECTION.override("work")
    assert _captured_command(p) == ["quarry", "--db", "work", "disable", p]


def test_captured_note_command_quotes_a_path_with_spaces_and_a_quote(p: str) -> None:
    parent = f"{p}/my proj/it's"
    assert _captured_command(parent) == ["quarry", "disable", parent]


@pytest.mark.usefixtures("db_override")
def test_captured_note_command_quotes_the_db_name(p: str) -> None:
    SELECTION.override("a b")
    assert _captured_command(p) == ["quarry", "--db", "a b", "disable", p]


def test_disable_report_covered_child_kept_config_is_not_nothing_found(p: str) -> None:
    result = DisableResult(
        directory=f"{p}/src",
        collection="",
        captures_collection="",
        covered_by=p,
        capturing_marker=p,
        config_kept=True,
    )
    lines = DisableReport(result, keep_data=False).lines()
    assert lines[0] == f"Nothing removed from {p}/src"
    assert lines[1].startswith("  Config file kept")
    assert lines[2] == _still_captured(p)
