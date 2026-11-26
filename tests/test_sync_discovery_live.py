"""Live == bulk: the per-path watch predicate agrees with the bulk walk.

``FileDiscovery.discover`` prunes ignored directories before it looks at any
file, so git's rule applies: a file under an excluded directory cannot be
re-included by a negation.  ``is_indexable`` must reach the same verdict for
every path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    from pathlib import Path

_EXTS = frozenset({".md", ".txt"})
_IGNORE_FILES = (".gitignore", ".quarryignore")


class TestAncestorExclusion:
    @pytest.fixture(params=_IGNORE_FILES)
    def ignore_name(self, request: pytest.FixtureRequest) -> str:
        return str(request.param)

    def _star_tree(self, tmp_path: Path, ignore_name: str) -> Path:
        root = tmp_path / "root"
        (root / "child").mkdir(parents=True)
        (root / ignore_name).write_text("*\n!AGENTS.md\n")
        (root / "AGENTS.md").write_text("root")
        (root / "child" / "AGENTS.md").write_text("child")
        return root

    def test_negated_file_under_excluded_dir_is_not_indexable(
        self, tmp_path: Path, ignore_name: str
    ):
        root = self._star_tree(tmp_path, ignore_name)
        assert not FileDiscovery(root).is_indexable(root / "child" / "AGENTS.md", _EXTS)

    def test_negated_file_at_root_is_indexable(self, tmp_path: Path, ignore_name: str):
        root = self._star_tree(tmp_path, ignore_name)
        assert FileDiscovery(root).is_indexable(root / "AGENTS.md", _EXTS)

    def test_discover_returns_only_root_file(self, tmp_path: Path, ignore_name: str):
        root = self._star_tree(tmp_path, ignore_name)
        assert FileDiscovery(root).discover(_EXTS) == [root / "AGENTS.md"]

    def test_directory_pattern_excludes_descendant_file(
        self, tmp_path: Path, ignore_name: str
    ):
        root = tmp_path / "root"
        (root / "build").mkdir(parents=True)
        (root / ignore_name).write_text("build/\n")
        (root / "build" / "notes.md").write_text("x")
        assert not FileDiscovery(root).is_indexable(root / "build" / "notes.md", _EXTS)

    def test_negation_cannot_reinclude_nested_dir_of_excluded_dir(
        self, tmp_path: Path, ignore_name: str
    ):
        root = tmp_path / "root"
        (root / "build" / "keep").mkdir(parents=True)
        (root / ignore_name).write_text("build/\n!build/keep/\n")
        keep = root / "build" / "keep" / "a.md"
        keep.write_text("x")
        assert not FileDiscovery(root).is_indexable(keep, _EXTS)


class TestLiveAgreesWithBulk:
    def _build(self, root: Path) -> None:
        files = {
            ".gitignore": "*.log\ndocs/private/\n!docs/private/open.md\n",
            ".quarryignore": "vendor-notes/\n*.tmp.md\n!keep.tmp.md\n",
            "a.md": "",
            "keep.tmp.md": "",
            "drop.tmp.md": "",
            "debug.log.md": "",
            "docs/b.md": "",
            "docs/private/open.md": "",
            "docs/private/secret.md": "",
            "docs/public/c.txt": "",
            "docs/public/.gitignore": "hidden-in-public.md\n!c.txt\n",
            "docs/public/hidden-in-public.md": "",
            "docs/public/deep/d.md": "",
            "vendor-notes/e.md": "",
            "src/pkg/f.md": "",
            "src/pkg/.gitignore": "gen/\n",
            "src/pkg/gen/g.md": "",
            "src/pkg/gen/inner/h.md": "",
            "node_modules/i.md": "",
            ".hidden/j.md": "",
            "x/y/z/k.md": "",
            "x/.gitignore": "y/z/\n",
        }
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (root / "link-ok.md").symlink_to(root / "a.md")
        (root / "link-private.md").symlink_to(root / "docs/private/secret.md")
        (root / "link-gen.md").symlink_to(root / "src/pkg/gen/g.md")
        (root / "dirlink").symlink_to(root / "docs/public")

    def test_every_file_on_disk_gets_the_same_verdict(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        self._build(root)
        discovery = FileDiscovery(root)
        bulk = set(discovery.discover(_EXTS))
        on_disk = [p for p in root.rglob("*") if p.is_file() and p.suffix in _EXTS]
        on_disk += [root / "dirlink" / "c.txt", root / "dirlink" / "deep" / "d.md"]
        disagreements = {
            str(p.relative_to(root)): (discovery.is_indexable(p, _EXTS), p in bulk)
            for p in on_disk
            if discovery.is_indexable(p, _EXTS) != (p in bulk)
        }
        assert disagreements == {}


class TestSymlinks:
    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "root"
        (root / "secret").mkdir(parents=True)
        (root / "open").mkdir()
        (root / ".gitignore").write_text("secret/\n")
        (root / "secret" / "x.md").write_text("ignored text")
        (root / "open" / "y.md").write_text("fine")
        return root

    def test_file_link_into_ignored_directory_is_not_indexable(self, tmp_path: Path):
        root = self._root(tmp_path)
        (root / "pub.md").symlink_to(root / "secret" / "x.md")
        assert not FileDiscovery(root).is_indexable(root / "pub.md", _EXTS)

    def test_file_link_into_ignored_directory_is_not_discovered(self, tmp_path: Path):
        root = self._root(tmp_path)
        (root / "pub.md").symlink_to(root / "secret" / "x.md")
        assert root / "pub.md" not in FileDiscovery(root).discover(_EXTS)

    def test_file_link_to_kept_file_is_indexable_and_discovered(self, tmp_path: Path):
        root = self._root(tmp_path)
        (root / "ok.md").symlink_to(root / "open" / "y.md")
        discovery = FileDiscovery(root)
        assert discovery.is_indexable(root / "ok.md", _EXTS)
        assert root / "ok.md" in discovery.discover(_EXTS)

    @pytest.mark.parametrize("target", ["secret", "open"])
    def test_file_below_a_directory_link_is_not_indexable(
        self, tmp_path: Path, target: str
    ):
        root = self._root(tmp_path)
        (root / "pub").symlink_to(root / target)
        name = "x.md" if target == "secret" else "y.md"
        assert not FileDiscovery(root).is_indexable(root / "pub" / name, _EXTS)

    def test_directory_link_is_not_watchable(self, tmp_path: Path):
        root = self._root(tmp_path)
        (root / "pub").symlink_to(root / "open")
        assert not FileDiscovery(root).is_watchable_dir(root / "pub")
