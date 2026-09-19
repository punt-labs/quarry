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


class TestRepoAncestors:
    """The bounded walk both the pin lookup and the sidecar lookup share."""

    def test_ends_at_the_checkout_root_inclusive(self, unpinned_root: Path) -> None:
        child = unpinned_root / "child"
        deep = child / "src" / "pkg"
        deep.mkdir(parents=True)
        (child / ".git").mkdir()
        assert list(EthosTree(deep).repo_ancestors()) == [
            deep.resolve(),
            (child / "src").resolve(),
            child.resolve(),
        ]

    def test_home_is_skipped_but_still_bounds_the_walk(
        self, unpinned_root: Path
    ) -> None:
        """A scratch directory under home walks past it without yielding it."""
        home = unpinned_root / "home"
        scratch = home / "scratch"
        scratch.mkdir(parents=True)
        with patch("quarry.ethos_tree.Path.home", return_value=home):
            chain = list(EthosTree(scratch).repo_ancestors())
        assert home not in chain
        assert chain == [scratch.resolve(), unpinned_root.resolve()]

    def test_a_symlinked_home_is_still_skipped(self, unpinned_root: Path) -> None:
        """``$HOME`` may be a symlink; the ancestors are resolved, so resolve it too.

        Comparing an unresolved home against resolved ancestors never matches,
        and the global ``~/.punt-labs`` would then be read as a repo's pin.
        """
        real_home = unpinned_root / "real-home"
        scratch = real_home / "scratch"
        scratch.mkdir(parents=True)
        linked_home = unpinned_root / "home-link"
        linked_home.symlink_to(real_home)
        with patch("quarry.ethos_tree.Path.home", return_value=linked_home):
            chain = list(EthosTree(scratch).repo_ancestors())
            pins = list(EthosTree(scratch).pin_files())
        assert real_home.resolve() not in chain
        assert chain == [scratch.resolve(), unpinned_root.resolve()]
        assert not any(
            pin.is_relative_to(real_home.resolve() / ".punt-labs") for pin in pins
        )


class TestPinFiles:
    def test_new_file_precedes_legacy_at_each_ancestor(self, tmp_path: Path) -> None:
        first, second, *_ = EthosTree(tmp_path).pin_files()
        assert first == tmp_path.resolve() / ".punt-labs" / "ethos.yaml"
        assert second == tmp_path.resolve() / ".punt-labs" / "ethos" / "config.yaml"

    def test_candidates_are_not_checked_for_existence(self, tmp_path: Path) -> None:
        assert not any(
            pin.exists() for pin in list(EthosTree(tmp_path).pin_files())[:2]
        )

    def test_candidates_stop_at_the_checkout_root(self, unpinned_root: Path) -> None:
        """A parent directory's pin is never a candidate for a repo's session.

        Attribution reads the pin of the checkout a session is opened in; the
        walk that finds it is bounded exactly like the vendored-tree walk, so a
        workspace meta-repo's pin cannot name the identity of a child repo.
        """
        child = unpinned_root / "child"
        (child / ".git").mkdir(parents=True)
        candidates = list(EthosTree(child / "src").pin_files())
        assert candidates
        assert all(pin.is_relative_to(child.resolve()) for pin in candidates)

    def test_home_is_never_a_candidate(self, unpinned_root: Path) -> None:
        """``~/.punt-labs`` is the global tree; its files are not a repo pin."""
        home = unpinned_root / "home"
        scratch = home / "scratch"
        scratch.mkdir(parents=True)
        with patch("quarry.ethos_tree.Path.home", return_value=home):
            candidates = list(EthosTree(scratch).pin_files())
        assert candidates
        assert not any(pin.is_relative_to(home / ".punt-labs") for pin in candidates)


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

    def test_symlinked_vendored_identity_is_not_an_identity(
        self, unpinned_root: Path, tmp_path: Path
    ) -> None:
        """A committed link to a file outside the checkout registers nothing.

        Otherwise SubagentStop would file ``memory-<handle>`` for a handle the
        repo never vendored — attribution decided by wherever the link points.
        """
        outside = tmp_path / "outside-rmh.yaml"
        outside.write_text("name: rmh\n")
        identities = _vendor_identity(unpinned_root, "claude")
        (identities / "rmh.yaml").symlink_to(outside)
        assert (identities / "rmh.yaml").is_file()  # what a naive probe says
        assert EthosTree(unpinned_root).identity_exists("rmh") is False
        assert EthosTree(unpinned_root).identity_exists("claude") is True

    def test_symlinked_vendored_identities_directory_is_not_a_tree(
        self, unpinned_root: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        _vendor_identity(other, "rmh")
        ethos = unpinned_root / ".punt-labs" / "ethos"
        ethos.mkdir(parents=True)
        (ethos / "identities").symlink_to(other / ".punt-labs" / "ethos" / "identities")
        assert EthosTree(unpinned_root).identity_exists("rmh") is False

    def test_symlinked_global_identity_is_honoured(
        self, unpinned_root: Path, tmp_path: Path
    ) -> None:
        """The global tree is the operator's: a dotfile-manager link counts."""
        outside = tmp_path / "dotfiles-kpz.yaml"
        outside.write_text("name: kpz\n")
        home = EthosTree.global_identities()
        home.mkdir(parents=True, exist_ok=True)
        link = home / "kpz.yaml"
        link.symlink_to(outside)
        try:
            assert EthosTree(unpinned_root).identity_exists("kpz") is True
        finally:
            link.unlink()


class TestCheckoutRoot:
    def test_strips_the_sidecar_layout(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "repo" / ".punt-labs" / "ethos" / "missions"
        assert EthosTree.checkout_root(sidecar) == tmp_path / "repo"

    def test_pin_files_root_at_their_ancestor(self, tmp_path: Path) -> None:
        """Both pin shapes seal at the directory that holds ``.punt-labs``."""
        repo = tmp_path / "repo"
        assert EthosTree.checkout_root(repo / ".punt-labs" / "ethos.yaml") == repo
        assert (
            EthosTree.checkout_root(repo / ".punt-labs" / "ethos" / "config.yaml")
            == repo
        )

    def test_a_file_deep_in_the_sidecar_roots_at_the_checkout(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        deep = repo / ".punt-labs" / "ethos" / "identities" / "rmh.yaml"
        assert EthosTree.checkout_root(deep) == repo

    def test_a_path_outside_any_sidecar_is_a_bug(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"\.punt-labs"):
            EthosTree.checkout_root(tmp_path / "repo" / "src" / "x.py")

    def test_agrees_with_the_locator(self, unpinned_root: Path) -> None:
        identities = _vendor_identity(unpinned_root, "rmh")
        located = EthosTree(unpinned_root / "src").vendored_identities()
        assert located == identities
        assert EthosTree.checkout_root(identities) == unpinned_root
