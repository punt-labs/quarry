"""Tests for CollectionResolver: covering, re-adopt, and unique-name resolution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from quarry.collection_resolver import CollectionResolver
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    import pytest

_NO_CHUNKS: frozenset[str] = frozenset()


class TestUniqueCollectionName:
    def test_uses_leaf_name_when_available(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "myproject"
        project.mkdir()
        name = CollectionResolver(conn).unique_collection_name(project, _NO_CHUNKS)
        assert name == "myproject"
        conn.close()

    def test_disambiguates_with_parent(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "r.db")
        # Register a different directory with the same leaf name.
        other = tmp_path / "other" / "myproject"
        other.mkdir(parents=True)
        conn.register_directory(other, "myproject")

        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)
        name = CollectionResolver(conn).unique_collection_name(project, _NO_CHUNKS)
        assert name == "myproject-mine"
        conn.close()

    def test_falls_back_to_hash_on_double_collision(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "r.db")
        # Occupy both "myproject" and "myproject-mine".
        d1 = tmp_path / "a" / "myproject"
        d1.mkdir(parents=True)
        conn.register_directory(d1, "myproject")

        d2 = tmp_path / "b" / "myproject"
        d2.mkdir(parents=True)
        conn.register_directory(d2, "myproject-mine")

        project = tmp_path / "mine" / "myproject"
        project.mkdir(parents=True)
        name = CollectionResolver(conn).unique_collection_name(project, _NO_CHUNKS)
        assert name.startswith("myproject-")
        assert len(name) == len("myproject-") + 8  # 8-char hash
        conn.close()

    def test_root_dir_falls_back_to_nonempty_leaf(self, tmp_path: Path) -> None:
        # A filesystem-root directory has an empty .name; the collection must
        # never be registered with an empty name — the leaf falls back to "root".
        conn = SyncRegistry(tmp_path / "r.db")
        name = CollectionResolver(conn).unique_collection_name(Path("/"), _NO_CHUNKS)
        assert name == "root"
        conn.close()

    def test_root_dir_collision_disambiguates_off_root_leaf(
        self, tmp_path: Path
    ) -> None:
        # With "root" taken, the root dir disambiguates off the "root" leaf
        # (never off an empty string).
        conn = SyncRegistry(tmp_path / "r.db")
        conn.register_directory(tmp_path, "root")

        name = CollectionResolver(conn).unique_collection_name(Path("/"), _NO_CHUNKS)
        assert name.startswith("root-")
        assert name != "root-"
        conn.close()

    def test_avoids_chunk_bearing_captures_or_memory_name(self, tmp_path: Path) -> None:
        # No LIVE registration and no retained archive named "default", but the
        # daemon reports it as chunk-bearing (a captures/memory/remember target
        # such as "default-captures", "memory-x", or "default").  A new, unrelated
        # directory whose leaf is "default" must NOT be handed that chunk-bearing
        # name — it disambiguates off the parent.  FAILS against a registry-only
        # picker (which returns "default"); passes once chunk names join the set.
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "acme" / "default"
        project.mkdir(parents=True)

        name = CollectionResolver(conn).unique_collection_name(
            project, frozenset({"default"})
        )
        assert name == "default-acme"
        conn.close()

    def test_avoids_subsumed_evicted_child_chunks(self, tmp_path: Path) -> None:
        # A parent registration once subsumed-and-evicted a child ("backend"); its
        # directory row is gone but its chunks remain in LanceDB (drained lazily by
        # the orphan sweep).  A DIFFERENT directory whose leaf is "backend" must not
        # claim that still-chunk-bearing name and merge into the evicted child's
        # chunks — with "backend" in the chunk set, it disambiguates instead.
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "acme" / "backend"
        project.mkdir(parents=True)

        name = CollectionResolver(conn).unique_collection_name(
            project, frozenset({"backend"})
        )
        assert name == "backend-acme"
        conn.close()

    def test_hash_fallback_avoids_chunk_bearing_name(self, tmp_path: Path) -> None:
        # Leaf, leaf-parent, AND the 8-char hash candidate all already hold chunks.
        # The fallback must be avoid-checked too, so the hash suffix lengthens
        # until it clears every chunk-bearing name.
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "acme" / "backend"
        project.mkdir(parents=True)
        digest = hashlib.sha256(str(project).encode()).hexdigest()
        taken = frozenset({"backend", "backend-acme", f"backend-{digest[:8]}"})

        name = CollectionResolver(conn).unique_collection_name(project, taken)
        assert name not in taken
        assert name.startswith("backend-")
        conn.close()


class TestArchivedCollectionFor:
    """A directory re-adopts an archive it owns, like `quarry enable` does."""

    def test_owning_directory_readopts_its_archive(self, tmp_path: Path) -> None:
        # A keep-data disable archives "myproject" under its original directory.
        # The SAME directory reuses that name — not a fresh disambiguated one —
        # so its kept chunks are re-adopted.
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "myproject"
        project.mkdir()
        conn.register_directory(project, "myproject")
        conn.deregister_directory("myproject", keep_data=True)

        assert CollectionResolver(conn).archived_collection_for(project) == "myproject"
        conn.close()

    def test_same_dir_readopt_unaffected_by_chunk_picker(self, tmp_path: Path) -> None:
        # The SAME directory that owns a keep-data archive re-adopts it by name,
        # even though that name is chunk-bearing (its kept chunks are exactly why
        # it re-adopts).  archived_collection_for runs BEFORE the chunk-avoiding
        # picker, so the re-adopt path never reaches (and is never blocked by) the
        # tightened unique_collection_name.
        conn = SyncRegistry(tmp_path / "r.db")
        project = tmp_path / "work" / "backend"
        project.mkdir(parents=True)
        conn.register_directory(project, "backend")
        conn.deregister_directory("backend", keep_data=True)

        resolver = CollectionResolver(conn)
        # The same directory owns the archive → re-adopt "backend" verbatim.
        assert resolver.archived_collection_for(project) == "backend"
        # And had it fallen through to the picker (it does not), the chunk-bearing
        # name would have been avoided — proving the ordering is what preserves it.
        picked = resolver.unique_collection_name(project, frozenset({"backend"}))
        assert picked != "backend"
        conn.close()

    def test_unrelated_directory_owns_no_archive(self, tmp_path: Path) -> None:
        # A DIFFERENT directory with the same leaf owns no archive → None, so it
        # falls through to a fresh unique name (I7: no cross-project adoption).
        conn = SyncRegistry(tmp_path / "r.db")
        owner = tmp_path / "work" / "myproject"
        owner.mkdir(parents=True)
        conn.register_directory(owner, "myproject")
        conn.deregister_directory("myproject", keep_data=True)

        other = tmp_path / "other" / "myproject"
        other.mkdir(parents=True)
        assert CollectionResolver(conn).archived_collection_for(other) is None
        conn.close()


class TestRemoteLocalParity:
    """The local resolver and the remote client pick the same name for one state."""

    def test_picker_result_matches_registrations(self, tmp_path: Path) -> None:
        # Build one shared state — a live registration, a keep-data archive, and a
        # chunk-bearing captures collection — then assert the local CollectionResolver
        # and the remote Registrations view pick the SAME fresh name for the same
        # new directory.  This is the merge-proof invariant's parity guarantee: a
        # divergence between the two surfaces is exactly the bug class this closes.
        from quarry.api import RegistrationInfo, RegistrationList, RetainedCollection
        from quarry.registrations import Registrations

        conn = SyncRegistry(tmp_path / "r.db")
        live_dir = tmp_path / "live" / "backend"
        live_dir.mkdir(parents=True)
        conn.register_directory(live_dir, "backend")
        archived_dir = tmp_path / "old" / "backend-live"
        archived_dir.mkdir(parents=True)
        conn.register_directory(archived_dir, "backend-live")
        conn.deregister_directory("backend-live", keep_data=True)

        # A new, unrelated directory whose leaf ("backend") collides with the live
        # registration; "backend-captures" is chunk-bearing.
        new_dir = tmp_path / "new" / "backend"
        new_dir.mkdir(parents=True)
        chunk_collections = ["backend", "backend-captures"]

        local = CollectionResolver(conn).unique_collection_name(
            new_dir, frozenset(chunk_collections)
        )

        # The remote view built from the SAME state (registrations + retained +
        # chunk_collections) must pick identically.
        listing = RegistrationList(
            total_registrations=1,
            registrations=[
                RegistrationInfo(
                    collection="backend",
                    directory=str(live_dir.resolve()),
                    registered_at="2026-01-01",
                )
            ],
            retained=[
                RetainedCollection(
                    collection="backend-live",
                    original_directory=str(archived_dir.resolve()),
                )
            ],
            chunk_collections=chunk_collections,
        )
        remote = Registrations.from_list(listing).unique_collection_name(new_dir)

        assert local == remote
        conn.close()


class TestCoveringCollection:
    def test_returns_collection_for_exact_match(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        conn = SyncRegistry(tmp_path / "registry.db")
        conn.register_directory(project, "myproject")
        assert CollectionResolver(conn).covering_collection(str(project)) == "myproject"
        conn.close()

    def test_returns_collection_for_subdirectory(self, tmp_path: Path) -> None:
        project = tmp_path / "myproject"
        project.mkdir()
        subdir = project / "src" / "lib"
        subdir.mkdir(parents=True)
        conn = SyncRegistry(tmp_path / "registry.db")
        conn.register_directory(project, "myproject")
        assert CollectionResolver(conn).covering_collection(str(subdir)) == "myproject"
        conn.close()

    def test_returns_none_for_unregistered_directory(self, tmp_path: Path) -> None:
        conn = SyncRegistry(tmp_path / "registry.db")
        resolver = CollectionResolver(conn)
        assert resolver.covering_collection(str(tmp_path / "unregistered")) is None
        conn.close()

    def test_relative_cwd_is_not_covered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "myproject"
        (project / "src").mkdir(parents=True)
        conn = SyncRegistry(tmp_path / "registry.db")
        conn.register_directory(project, "myproject")
        monkeypatch.chdir(project)
        assert CollectionResolver(conn).covering_collection("src") is None
        conn.close()
