"""Quarry's ignore decision equals git's, checked against ``git check-ignore``.

Every case builds a tree, asks real git which ``.md`` files it ignores, and
asserts that :meth:`FileDiscovery.is_indexable` and membership in
:meth:`FileDiscovery.discover` both say the opposite.  ``.quarryignore`` has no
git counterpart, so the same trees are rebuilt with that file name and must
give git's answer for the ``.gitignore`` version.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_EXTS = frozenset({".md"})
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

_CASES: dict[str, dict[str, str]] = {
    "nested file applies at every depth below it": {
        "child/.gitignore": "secret.md\ndocs/private/\n**/star.md\n",
        "child/secret.md": "",
        "child/a/secret.md": "",
        "child/docs/private/p.md": "",
        "child/a/docs/private/p.md": "",
        "child/a/b/star.md": "",
        "child/keep.md": "",
    },
    "root allow-list with re-included directories": {
        ".gitignore": "*\n!*/\n!AGENTS.md\n",
        "AGENTS.md": "",
        "child/AGENTS.md": "",
        "child/other.md": "",
        "child/deep/AGENTS.md": "",
        "top.md": "",
    },
    "root allow-list without re-including directories": {
        ".gitignore": "*\n!AGENTS.md\n",
        "AGENTS.md": "",
        "child/AGENTS.md": "",
    },
    "anchored pattern matches only at its own level": {
        ".gitignore": "/top.md\n",
        "top.md": "",
        "sub/top.md": "",
        "sub/.gitignore": "/own.md\n",
        "sub/own.md": "",
        "sub/deeper/own.md": "",
    },
    "directory pattern": {
        ".gitignore": "out/\nlogs\n",
        "out/a.md": "",
        "x/out/a.md": "",
        "logs/a.md": "",
        "x/logs/a.md": "",
        "outfile.md": "",
    },
    "double star patterns": {
        ".gitignore": "**/gen/*.md\na/**/z.md\ntail/**\n",
        "gen/a.md": "",
        "p/gen/a.md": "",
        "p/gen/q/a.md": "",
        "a/z.md": "",
        "a/b/c/z.md": "",
        "b/z.md": "",
        "tail/a.md": "",
        "tail/x/y.md": "",
    },
    "negation in the same file re-includes what it excluded": {
        ".gitignore": "*.md\n!keep.md\n",
        "keep.md": "",
        "drop.md": "",
        "d/keep.md": "",
        "d/drop.md": "",
    },
    "negation in a child file re-includes what a parent excluded by name": {
        ".gitignore": "note.md\n",
        "sub/.gitignore": "!note.md\n",
        "note.md": "",
        "sub/note.md": "",
        "sub/deeper/note.md": "",
    },
    "negation in a child file cannot re-include below an excluded directory": {
        ".gitignore": "private/\n",
        "private/.gitignore": "!open.md\n",
        "private/open.md": "",
        "private/sub/open.md": "",
        "other/open.md": "",
    },
    "negation of a file under an excluded directory in the same file": {
        ".gitignore": "private/\n!private/open.md\n",
        "private/open.md": "",
        "other.md": "",
    },
    "deeper file overrides shallower file": {
        ".gitignore": "*.md\n",
        "a/.gitignore": "!x.md\n",
        "a/b/.gitignore": "x.md\n",
        "a/x.md": "",
        "a/y.md": "",
        "a/b/x.md": "",
        "a/b/c/x.md": "",
    },
    "character classes and question marks": {
        ".gitignore": "n[0-9].md\nq?.md\n[!a-c]z.md\n",
        "n1.md": "",
        "nx.md": "",
        "q1.md": "",
        "q12.md": "",
        "az.md": "",
        "dz.md": "",
    },
    "escapes, comments and trailing spaces": {
        ".gitignore": "# comment.md\n\\#hash.md\n\\!bang.md\nspace.md  \n",
        "# comment.md": "",
        "#hash.md": "",
        "!bang.md": "",
        "space.md": "",
    },
    "pattern with an inner slash is anchored": {
        ".gitignore": "docs/private.md\n",
        "docs/private.md": "",
        "x/docs/private.md": "",
        "private.md": "",
    },
}


def _write_tree(root: Path, files: dict[str, str], ignore_name: str) -> None:
    root.mkdir(parents=True)
    for rel, text in files.items():
        target = rel
        if rel.endswith(".gitignore"):
            target = rel.removesuffix(".gitignore") + ignore_name
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _git_ignored(root: Path, rels: list[str]) -> set[str]:
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=_GIT_ENV)
    done = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--stdin", "-z", "-n", "-v"],
        input="\0".join(rels) + "\0",
        capture_output=True,
        text=True,
        check=False,
        env=_GIT_ENV,
    )
    assert done.returncode in (0, 1), done.stderr
    ignored: set[str] = set()
    fields = done.stdout.split("\0")
    # ``-v -n`` prints source, line, pattern, path per input, matched or not.
    for index in range(0, len(fields) - 3, 4):
        source, _line, pattern, path = fields[index : index + 4]
        negated = pattern.startswith("!")
        if source and not negated:
            ignored.add(path)
    return ignored


@pytest.mark.parametrize("ignore_name", [".gitignore", ".quarryignore"])
@pytest.mark.parametrize("case", list(_CASES))
def test_quarry_ignores_exactly_what_git_ignores(
    tmp_path: Path, case: str, ignore_name: str
) -> None:
    files = _CASES[case]
    markdown = sorted(rel for rel in files if rel.endswith(".md"))
    git_root = tmp_path / "git"
    _write_tree(git_root, files, ".gitignore")
    expected = set(markdown) - _git_ignored(git_root, markdown)

    root = tmp_path / "quarry"
    _write_tree(root, files, ignore_name)
    discovery = FileDiscovery(root)
    scanned = {str(p.relative_to(root)) for p in discovery.discover(_EXTS)}
    live = {rel for rel in markdown if discovery.is_indexable(root / rel, _EXTS)}

    assert scanned == expected, "discover() differs from git"
    assert live == expected, "is_indexable() differs from git"
