"""Tests for the clause saying how long session start stays active."""

from __future__ import annotations

from pathlib import Path

from quarry.marker_chain import MarkerChain


def _opt_out(directory: Path) -> str:
    config = directory / ".punt-labs/quarry/config.md"
    config.parent.mkdir(parents=True)
    config.write_text("---\nauto_capture:\n  session_sync: false\n---\n")
    return str(directory)


def test_all_markers_syncing_stays_active_until_all_are_disabled(
    tmp_path: Path,
) -> None:
    markers = (str(tmp_path / "a"), str(tmp_path / "b"))
    text = MarkerChain(markers, "`quarry disable a`").duration()
    assert text == (
        "it stays active until all of them are disabled (`quarry disable a`)"
    )


def test_next_marker_opt_out_stops_it_whatever_a_farther_marker_allows(
    tmp_path: Path,
) -> None:
    nearest, nxt, far = (tmp_path / n for n in ("a", "b", "c"))
    stop = _opt_out(nxt)
    text = MarkerChain((str(nearest), stop, str(far)), "`quarry disable a`").duration()
    assert f"session_sync is false in the config at {stop}" in text
    assert "whatever a farther marker allows" in text
    assert "(disabling each: `quarry disable a`)" in text


def test_nearest_marker_opt_out_is_not_this_chains_concern(tmp_path: Path) -> None:
    nearest = _opt_out(tmp_path / "a")
    text = MarkerChain((nearest, str(tmp_path / "b")), "").duration()
    assert text == "it stays active until all of them are disabled"
