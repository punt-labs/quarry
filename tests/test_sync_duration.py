"""Tests for the clause saying how long session start stays active."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from quarry.parent_disable_command import ParentDisableCommand
from quarry.sync_duration import SyncDuration


def test_all_markers_syncing_stays_active_until_all_are_disabled(
    tmp_path: Path,
) -> None:
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    text = str(SyncDuration((a, b), b))
    assert text == (
        f"it stays active until all of them are disabled (`{ParentDisableCommand(a)}`)"
    )


def test_opt_out_lists_only_commands_nearer_than_it(
    tmp_path: Path, opt_out: Callable[[Path], str]
) -> None:
    near, mid, far = (tmp_path / n for n in ("a", "b", "c"))
    stop = opt_out(mid)
    text = str(SyncDuration((str(near), stop, str(far)), str(far)))
    assert f"session_sync is false in the config at {stop}" in text
    assert "whatever a farther marker allows" in text
    assert f"(disabling each: `{ParentDisableCommand(str(near))}`)" in text
    assert str(ParentDisableCommand(stop)) not in text
    assert str(ParentDisableCommand(str(far))) not in text


def test_nearest_marker_opt_out_is_not_this_clauses_concern(
    tmp_path: Path, opt_out: Callable[[Path], str]
) -> None:
    nearest, b = opt_out(tmp_path / "a"), str(tmp_path / "b")
    text = str(SyncDuration((nearest, b), ""))
    assert text == (
        f"it stays active until all of them are disabled "
        f"(`{ParentDisableCommand(nearest)}`, `{ParentDisableCommand(b)}`)"
    )
