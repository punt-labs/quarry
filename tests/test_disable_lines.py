"""Tests for the report lines describing a disable's local file changes."""

from __future__ import annotations

from pathlib import Path

from quarry.claude_permissions import PermissionChange
from quarry.disable_lines import DisableLines
from quarry.enable import DisableResult
from quarry.session_notes import SessionNote


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


def _opt_out(directory: Path) -> None:
    config = directory / ".punt-labs/quarry/config.md"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("---\nauto_capture:\n  session_sync: false\n---\n")


def _covered(
    tmp_path: Path, *markers: str, covered_by: str = ""
) -> tuple[Path, DisableResult]:
    parent = tmp_path / "p"
    child = parent / "team" / "svc"
    child.mkdir(parents=True)
    paths = [str(parent / m) if m else str(parent) for m in markers]
    result = DisableResult(
        directory=str(child),
        collection="",
        captures_collection="",
        covered_by=covered_by or str(parent),
        config_removed=False,
        config_kept=True,
        config_why="",
        capturing_marker=paths[0] if paths else "",
        also_capturing=tuple(paths[1:]),
        import_pruned=False,
        enabled_marker_removed=False,
        permissions=PermissionChange(),
    )
    return parent, result


def test_child_opted_out_of_session_sync_is_not_called_active(tmp_path: Path) -> None:
    _, result = _covered(tmp_path, "")
    _opt_out(Path(result.directory))
    note = str(SessionNote.of(result))
    assert "sync runs" not in note
    assert "still active" not in note
    assert "does not activate quarry" in note
    assert f"session_sync is false in the config at {result.directory}" in note
    assert "deregisters" not in note


def test_governing_marker_opted_out_is_not_called_active(tmp_path: Path) -> None:
    parent, result = _covered(tmp_path, "team", "")
    _opt_out(parent / "team")
    note = str(SessionNote.of(result))
    assert "sync runs" not in note
    assert "does not activate quarry" in note
    assert f"session_sync is false in the config at {parent / 'team'}" in note


def test_opted_out_note_keeps_the_destructive_warning_out(tmp_path: Path) -> None:
    parent, result = _covered(tmp_path, "")
    _opt_out(parent)
    note = str(SessionNote.of(result))
    assert "purges" not in note


def test_no_opt_out_keeps_the_sync_claim(tmp_path: Path) -> None:
    parent, result = _covered(tmp_path, "team")
    note = str(SessionNote.of(result))
    assert f"is still active through the marker at {parent / 'team'}" in note
    assert "(session-start sync runs there)" in note


def test_several_markers_none_opted_out_names_every_marker(tmp_path: Path) -> None:
    parent, result = _covered(tmp_path, "team", "")
    note = str(SessionNote.of(result))
    assert f"the markers at {parent / 'team'}, {parent}" in note
    assert "(session-start sync runs there)" in note


def test_unreadable_config_is_fail_open_and_sync_stays_on(tmp_path: Path) -> None:
    parent, result = _covered(tmp_path, "team")
    config = parent / "team/.punt-labs/quarry/config.md"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"\xff\xfe\x00")
    assert "sync runs there" in str(SessionNote.of(result))
