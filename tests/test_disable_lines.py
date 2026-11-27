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
    config_why: str = "",
    capturing_marker: str = "",
    also_capturing: tuple[str, ...] = (),
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
        config_why=config_why,
        capturing_marker=capturing_marker,
        also_capturing=also_capturing,
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


def test_unreadable_kept_config_says_it_is_not_applied() -> None:
    [line] = DisableLines(_result(config_kept=True, config_why="unreadable")).kept()
    assert "could not be read" in line
    assert "not applied" in line
    assert "settings of yours" not in line


def test_linked_config_says_it_was_left_in_place() -> None:
    assert DisableLines(_result(config_kept=True, config_why="linked")).kept() == [
        (
            "  config.md is behind a symlink (the file or a parent directory) "
            "and was left in place"
        )
    ]


def test_captured_note_at_the_registration_names_its_disable_command() -> None:
    note = DisableLines(
        _result(covered_by="/my p", capturing_marker="/my p")
    ).captured()
    assert "`quarry disable '/my p'` ends it but deregisters" in note


def test_captured_note_at_an_intermediate_marker_names_that_directory() -> None:
    note = DisableLines(
        _result(covered_by="/p", capturing_marker="/p/my team")
    ).captured()
    assert "still active through the marker at /p/my team" in note
    assert "`quarry disable '/p/my team'` ends it" in note
    assert "purges" not in note


def test_captured_note_with_an_intermediate_and_the_registration_lists_both() -> None:
    note = DisableLines(
        _result(covered_by="/p", capturing_marker="/p/a", also_capturing=("/p",))
    ).captured()
    assert "still active through the markers at /p/a, /p" in note
    assert "until all of them are disabled" in note
    assert "`quarry disable /p/a`" in note
    assert "`quarry disable /p` deregisters /p and purges everything indexed" in note
    assert "--keep-data" in note
    assert "ends it, as does" not in note


def test_captured_note_with_two_intermediates_lists_each_command() -> None:
    note = DisableLines(
        _result(
            covered_by="/p",
            capturing_marker="/p/a/b",
            also_capturing=("/p/my a",),
        )
    ).captured()
    assert "still active through the markers at /p/a/b, /p/my a" in note
    assert "`quarry disable /p/a/b`" in note
    assert "`quarry disable '/p/my a'`" in note
    assert "until all of them are disabled" in note


def test_captured_note_with_one_marker_is_unchanged() -> None:
    note = DisableLines(_result(covered_by="/p", capturing_marker="/p/a")).captured()
    assert "still active through the marker at /p/a" in note
    assert "until all" not in note
