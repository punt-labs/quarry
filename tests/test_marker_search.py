"""Tests for the search that finds the marker governing a directory."""

from __future__ import annotations

from pathlib import Path

from quarry.enabled_marker import EnabledMarker
from quarry.marker_search import MarkerSearch


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "work"
    child = root / "app"
    deep = child / "src" / "deep"
    deep.mkdir(parents=True)
    return root, child, deep


def test_no_registration_checks_only_the_directory(tmp_path: Path) -> None:
    _, child, deep = _dirs(tmp_path)
    EnabledMarker(child).write()
    assert MarkerSearch(deep, "").found() is False
    assert MarkerSearch(child, "").found() is True


def test_climbs_from_a_subdirectory_to_the_enabled_child(tmp_path: Path) -> None:
    root, child, deep = _dirs(tmp_path)
    EnabledMarker(child).write()
    assert MarkerSearch(deep, str(root)).found() is True


def test_finds_the_registration_root_marker(tmp_path: Path) -> None:
    root, _, deep = _dirs(tmp_path)
    EnabledMarker(root).write()
    assert MarkerSearch(deep, str(root)).found() is True


def test_does_not_climb_above_the_registration_root(tmp_path: Path) -> None:
    root, _, deep = _dirs(tmp_path)
    EnabledMarker(tmp_path).write()
    assert MarkerSearch(deep, str(root)).found() is False


def test_root_not_above_the_directory_is_still_checked(tmp_path: Path) -> None:
    root, _, _ = _dirs(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    EnabledMarker(root).write()
    assert MarkerSearch(elsewhere, str(root)).found() is True
