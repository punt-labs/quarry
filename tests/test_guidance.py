"""Tests for repo guide deposit."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarry.guidance import REPO_IMPORT_LINE, Guidance


def _fake_files_raising(exc: type[BaseException]) -> Callable[[str], object]:
    """Stand in for ``importlib.resources.files`` where ``read_text`` raises."""

    def read_text(encoding: str) -> str:
        raise exc("missing packaged resource")

    resource = SimpleNamespace(read_text=read_text)
    traversable = SimpleNamespace(joinpath=lambda _resource: resource)
    return lambda _package: traversable


def test_repo_import_line_is_canonical() -> None:
    assert REPO_IMPORT_LINE == "@.punt-labs/quarry/CLAUDE.md"


def test_deposit_writes_guide_wholesale(tmp_path: Path) -> None:
    guidance = Guidance(tmp_path)
    guidance.deposit()
    text = guidance.guide_path.read_text()
    assert guidance.guide_path == tmp_path / ".punt-labs" / "quarry" / "CLAUDE.md"
    assert text.startswith("# Quarry\n")
    assert "/find" in text


def test_deposit_overwrites_hand_edit(tmp_path: Path) -> None:
    """The vendored zone is deterministic: a hand edit is replaced wholesale."""
    guidance = Guidance(tmp_path)
    guidance.deposit()
    guidance.guide_path.write_text("tampered\n")
    guidance.deposit()
    assert guidance.guide_path.read_text().startswith("# Quarry\n")


def test_deposit_refuses_symlinked_ancestor_and_spares_external(tmp_path: Path) -> None:
    """A symlinked ancestor must not overwrite a CLAUDE.md outside the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    (external / "quarry").mkdir(parents=True)
    victim = external / "quarry" / "CLAUDE.md"
    victim.write_text("external file the attacker wants clobbered\n")
    (repo / ".punt-labs").symlink_to(external)

    with pytest.raises(ValueError, match="ancestor"):
        Guidance(repo).deposit()

    assert victim.read_text() == "external file the attacker wants clobbered\n"


def test_deposit_raises_when_packaged_resource_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quarry.guidance.files", _fake_files_raising(FileNotFoundError))
    with pytest.raises(RuntimeError, match=r"quarry\.data:repo-guide\.md"):
        Guidance(tmp_path).deposit()


def test_deposit_raises_when_data_package_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "quarry.guidance.files", _fake_files_raising(ModuleNotFoundError)
    )
    with pytest.raises(RuntimeError, match=r"quarry\.data:repo-guide\.md"):
        Guidance(tmp_path).deposit()
