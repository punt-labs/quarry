"""Behaviour of :class:`quarry.ethos_tree.EthosTree`, the sidecar locator.

Absence tests build under ``unpinned_root``: ``tmp_path`` sits inside this
repo, whose own vendored ``.punt-labs/ethos/`` an ancestor walk would find.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.ethos_tree import EthosTree

if TYPE_CHECKING:
    from collections.abc import Iterator


def _vendor_identity(root: Path, handle: str) -> Path:
    """Deposit ``<root>/.punt-labs/ethos/identities/<handle>.yaml``; return the dir."""
    identities = root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    (identities / f"{handle}.yaml").write_text(f"name: {handle}\n")
    return identities


@pytest.fixture
def global_identity() -> Iterator[Path]:
    """Deposit ``kpz.yaml`` in the (redirected) global tree for one test."""
    home = EthosTree.global_identities()
    home.mkdir(parents=True, exist_ok=True)
    identity = home / "kpz.yaml"
    identity.write_text("name: kpz\n")
    yield identity
    identity.unlink(missing_ok=True)


class TestAncestors:
    def test_starts_at_cwd_and_ends_at_root(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        chain = list(EthosTree(deep).ancestors())
        assert chain[0] == deep.resolve()
        assert chain[-1] == Path(chain[-1].anchor)
        assert tmp_path.resolve() in chain

    def test_accepts_a_string_cwd(self, tmp_path: Path) -> None:
        assert next(EthosTree(str(tmp_path)).ancestors()) == tmp_path.resolve()


class TestPinFiles:
    def test_new_file_precedes_legacy_at_each_ancestor(self, tmp_path: Path) -> None:
        first, second, *_ = EthosTree(tmp_path).pin_files()
        assert first == tmp_path.resolve() / ".punt-labs" / "ethos.yaml"
        assert second == tmp_path.resolve() / ".punt-labs" / "ethos" / "config.yaml"

    def test_candidates_are_not_checked_for_existence(self, tmp_path: Path) -> None:
        assert not any(
            pin.exists() for pin in list(EthosTree(tmp_path).pin_files())[:2]
        )


class TestNearest:
    def test_finds_the_vendored_tree_from_a_subdirectory(self, tmp_path: Path) -> None:
        identities = _vendor_identity(tmp_path, "rmh")
        deep = tmp_path / "src" / "quarry"
        deep.mkdir(parents=True)
        assert EthosTree(deep).vendored_identities() == identities.resolve()

    def test_closest_ancestor_wins(self, tmp_path: Path) -> None:
        _vendor_identity(tmp_path, "outer")
        inner = tmp_path / "inner"
        inner_identities = _vendor_identity(inner, "inner")
        assert EthosTree(inner).vendored_identities() == inner_identities.resolve()

    def test_none_when_absent(self, unpinned_root: Path) -> None:
        assert EthosTree(unpinned_root).vendored_identities() is None
        assert EthosTree(unpinned_root).missions_dir() is None

    def test_home_tree_is_global_not_vendored(self, unpinned_root: Path) -> None:
        """``~/.punt-labs/ethos/identities`` is never reported as a vendored tree.

        A scratch directory directly under the home (no repo of its own) walks
        through the home ancestor; the tree there is the global one.
        """
        home = unpinned_root / "home"
        global_tree = _vendor_identity(home, "jfreeman")
        scratch = home / "scratch"
        scratch.mkdir()
        with patch("quarry.ethos_tree.Path.home", return_value=home):
            assert EthosTree(scratch).vendored_identities() is None
            assert EthosTree(scratch).identities_dirs() == (global_tree,)

    def test_a_file_at_the_path_is_not_a_tree(self, unpinned_root: Path) -> None:
        ethos = unpinned_root / ".punt-labs" / "ethos"
        ethos.mkdir(parents=True)
        (ethos / "missions").write_text("not a directory")
        assert EthosTree(unpinned_root).missions_dir() is None

    def test_missions_dir_found_from_a_subdirectory(self, unpinned_root: Path) -> None:
        missions = unpinned_root / ".punt-labs" / "ethos" / "missions"
        missions.mkdir(parents=True)
        sub = unpinned_root / "src"
        sub.mkdir()
        assert EthosTree(sub).missions_dir() == missions.resolve()

    def test_search_stops_at_the_enclosing_git_root(self, unpinned_root: Path) -> None:
        """A vendored tree belongs to the repo that contains cwd, not a parent repo.

        Ethos reads the git root only; stopping there keeps ``quarry enable``
        from refreshing (and diffing) a workspace meta-repo's vendored tree
        when run inside one of its child repositories.
        """
        _vendor_identity(unpinned_root, "outer")
        child = unpinned_root / "child"
        (child / ".git").mkdir(parents=True)
        assert EthosTree(child / "src").vendored_identities() is None
        assert EthosTree(child).identity_exists("outer") is False

    def test_git_root_itself_is_searched(self, unpinned_root: Path) -> None:
        identities = _vendor_identity(unpinned_root, "rmh")
        assert EthosTree(unpinned_root).vendored_identities() == identities.resolve()

    def test_worktree_git_file_is_a_repo_boundary(self, unpinned_root: Path) -> None:
        _vendor_identity(unpinned_root, "outer")
        worktree = unpinned_root / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /elsewhere\n")
        assert EthosTree(worktree).vendored_identities() is None


class TestIdentitiesDirs:
    def test_vendored_then_global_when_both_present(self, tmp_path: Path) -> None:
        vendored = _vendor_identity(tmp_path, "rmh")
        home = EthosTree.global_identities()
        home.mkdir(parents=True, exist_ok=True)
        assert EthosTree(tmp_path).identities_dirs() == (vendored.resolve(), home)

    def test_only_present_trees_are_returned(self, tmp_path: Path) -> None:
        """A missing global tree is omitted, not returned as a dangling path."""
        vendored = _vendor_identity(tmp_path, "rmh")
        dirs = EthosTree(tmp_path).identities_dirs()
        assert dirs[0] == vendored.resolve()
        assert all(path.is_dir() for path in dirs)

    def test_global_identities_reads_home_at_call_time(self) -> None:
        assert EthosTree.global_identities() == (
            Path.home() / ".punt-labs" / "ethos" / "identities"
        )


class TestIdentityExists:
    def test_vendored_identity(self, unpinned_root: Path) -> None:
        _vendor_identity(unpinned_root, "rmh")
        assert EthosTree(unpinned_root).identity_exists("rmh") is True

    def test_unknown_handle(self, unpinned_root: Path) -> None:
        _vendor_identity(unpinned_root, "rmh")
        assert EthosTree(unpinned_root).identity_exists("general-purpose") is False

    @pytest.mark.usefixtures("global_identity")
    def test_global_identity(self, unpinned_root: Path) -> None:
        assert EthosTree(unpinned_root).identity_exists("kpz") is True

    @pytest.mark.parametrize("handle", ["../claude", "", "A B", "Claude", "a/b"])
    def test_invalid_handles_never_probe_the_filesystem(
        self, unpinned_root: Path, handle: str
    ) -> None:
        """A traversal, blank, or uppercase handle is rejected before any path."""
        _vendor_identity(unpinned_root, "claude")
        assert EthosTree.is_valid_handle(handle) is False
        assert EthosTree(unpinned_root).identity_exists(handle) is False

    def test_directory_named_like_an_identity_does_not_count(
        self, unpinned_root: Path
    ) -> None:
        identities = _vendor_identity(unpinned_root, "rmh")
        (identities / "ghost.yaml").mkdir()
        assert EthosTree(unpinned_root).identity_exists("ghost") is False


class TestCheckoutRoot:
    def test_strips_the_sidecar_layout(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "repo" / ".punt-labs" / "ethos" / "missions"
        assert EthosTree.checkout_root(sidecar) == tmp_path / "repo"

    def test_agrees_with_the_locator(self, unpinned_root: Path) -> None:
        identities = _vendor_identity(unpinned_root, "rmh")
        located = EthosTree(unpinned_root / "src").vendored_identities()
        assert located == identities
        assert EthosTree.checkout_root(identities) == unpinned_root
