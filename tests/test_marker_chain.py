"""Tests for the ordered markers the session-start hook reads."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from quarry.marker_chain import MarkerChain


def test_opt_out_finds_the_first_marker_from_start_that_opts_out(
    tmp_path: Path, opt_out: Callable[[Path], str]
) -> None:
    near = opt_out(tmp_path / "a")
    far = opt_out(tmp_path / "b")
    chain = MarkerChain((near, far))
    assert chain.opt_out() == near
    assert chain.opt_out(1) == far


def test_opt_out_is_empty_when_every_marker_syncs(tmp_path: Path) -> None:
    assert MarkerChain((str(tmp_path / "a"), str(tmp_path / "b"))).opt_out() == ""


def test_until_returns_the_markers_nearer_than_the_stop() -> None:
    assert MarkerChain(("a", "b", "c")).until("c") == ("a", "b")


def test_until_an_empty_stop_returns_every_marker() -> None:
    assert MarkerChain(("a", "b")).until("") == ("a", "b")
