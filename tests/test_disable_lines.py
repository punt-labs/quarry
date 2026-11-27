"""Tests for the report lines describing a disable's local file changes."""

from __future__ import annotations

from quarry.claude_permissions import PermissionChange
from quarry.disable_lines import DisableLines
from quarry.enable import DisableResult


def _result(
    *,
    covered_by: str = "",
    config_removed: bool = False,
    config_kept: bool = False,
    import_pruned: bool = False,
    marker: bool = False,
    permissions: PermissionChange | None = None,
) -> DisableResult:
    return DisableResult(
        directory="/p",
        collection="",
        captures_collection="",
        covered_by=covered_by,
        config_removed=config_removed,
        config_kept=config_kept,
        import_pruned=import_pruned,
        enabled_marker_removed=marker,
        permissions=permissions or PermissionChange(),
    )


def test_nothing_changed_has_no_lines() -> None:
    changes = DisableLines(_result())
    assert changes.removed() == []
    assert changes.problems() == []
    assert changes.kept() == []


def test_each_removal_has_a_line() -> None:
    changes = DisableLines(
        _result(config_removed=True, import_pruned=True, marker=True)
    )
    assert changes.removed() == [
        "  Config file removed",
        "  Removed @.punt-labs/quarry/CLAUDE.md from CLAUDE.md",
        "  Removed enabled marker (guide left dormant)",
    ]


def test_permissions_error_is_a_problem_not_a_removal() -> None:
    changes = DisableLines(_result(permissions=PermissionChange(error="denied")))
    assert changes.removed() == []
    assert changes.problems() != []


def test_kept_config_has_a_line() -> None:
    assert DisableLines(_result(config_kept=True)).kept() == [
        "  Config file kept (it has settings of yours; delete it to drop them)"
    ]


def test_parent_command_names_the_covering_directory() -> None:
    assert (
        DisableLines(_result(covered_by="/my p")).parent_command()
        == "quarry disable '/my p'"
    )
