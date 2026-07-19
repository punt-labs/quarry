"""Direct unit tests for the client-side ``Registrations`` coverage view.

These exercise ``covering`` (exact / parent / none / root-stop) and all three
tiers of ``unique_collection_name`` (leaf, leaf-parent on collision, hash suffix
on double collision) at the unit level — the daemon-view seam enable/disable
depend on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from quarry.api import RegistrationInfo
from quarry.registrations import Registrations


def _reg(collection: str, directory: Path) -> RegistrationInfo:
    return RegistrationInfo(
        collection=collection,
        directory=str(directory.resolve()),
        registered_at="2026-01-01",
    )


class TestCovering:
    def test_exact_match(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        view = Registrations([_reg("project", project)])

        found = view.covering(project)

        assert found is not None
        assert found.collection == "project"

    def test_parent_match(self, tmp_path: Path) -> None:
        parent = tmp_path / "project"
        parent.mkdir()
        child = parent / "src" / "pkg"
        child.mkdir(parents=True)
        view = Registrations([_reg("project", parent)])

        found = view.covering(child)

        assert found is not None
        assert found.collection == "project"
        assert found.directory == str(parent.resolve())

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        registered = tmp_path / "a"
        registered.mkdir()
        unrelated = tmp_path / "b"
        unrelated.mkdir()
        view = Registrations([_reg("a", registered)])

        assert view.covering(unrelated) is None

    def test_empty_view_returns_none(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()

        assert Registrations([]).covering(project) is None

    def test_root_stop_does_not_loop(self, tmp_path: Path) -> None:
        # A query at the filesystem root must terminate (parent == current) and
        # return None rather than spin — no registration covers "/".
        view = Registrations([_reg("project", tmp_path)])

        assert view.covering(Path(Path(tmp_path.anchor))) is None


class TestUniqueCollectionName:
    def test_leaf_when_no_collision(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        view = Registrations([])

        assert view.unique_collection_name(project) == "myproject"

    def test_leaf_parent_on_leaf_collision(self, tmp_path: Path) -> None:
        # "myproject" is taken → disambiguate with the parent dir name.
        parent = tmp_path / "acme"
        project = parent / "myproject"
        project.mkdir(parents=True)
        view = Registrations([_reg("myproject", tmp_path / "other")])

        assert view.unique_collection_name(project) == "myproject-acme"

    def test_root_dir_falls_back_to_nonempty_leaf(self) -> None:
        # A filesystem-root directory has an empty .name; the collection must
        # never be registered with an empty name — the leaf falls back to "root".
        view = Registrations([])

        assert view.unique_collection_name(Path("/")) == "root"

    def test_root_dir_collision_disambiguates_off_root_leaf(self) -> None:
        # With "root" taken, the root dir disambiguates off the "root" leaf
        # (never off an empty string).
        view = Registrations([_reg("root", Path("/"))])

        name = view.unique_collection_name(Path("/"))
        assert name.startswith("root-")
        assert name != "root-"

    def test_hash_suffix_on_double_collision(self, tmp_path: Path) -> None:
        # Both "myproject" and "myproject-acme" are taken → sha256 path suffix.
        parent = tmp_path / "acme"
        project = parent / "myproject"
        project.mkdir(parents=True)
        view = Registrations(
            [
                _reg("myproject", tmp_path / "x"),
                _reg("myproject-acme", tmp_path / "y"),
            ]
        )

        expected_suffix = hashlib.sha256(str(project).encode()).hexdigest()[:8]
        assert view.unique_collection_name(project) == f"myproject-{expected_suffix}"
