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
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from quarry.sync_discovery import FileDiscovery

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_EXTS = frozenset({".md"})
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

_CASES: dict[str, dict[str, str | bytes]] = {
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
    "posix class followed by a literal hyphen": {
        ".gitignore": "[[:punct:]-0].md\ndraft.md\n",
        "0.md": "",
        "-.md": "",
        "draft.md": "",
        "guide.md": "",
    },
    "reversed range matches only its low end": {
        ".gitignore": "[b-a].md\n[!b-a]x.md\n",
        "a.md": "",
        "b.md": "",
        "ax.md": "",
    },
    "ranges with escapes and edge hyphens": {
        ".gitignore": "[-a].md\n[a-]x.md\n[\\]]y.md\n[a-c-e]z.md\n",
        "-.md": "",
        "a.md": "",
        "b.md": "",
        "-x.md": "",
        "ax.md": "",
        "bx.md": "",
        "]y.md": "",
        "ay.md": "",
        "dz.md": "",
        "-z.md": "",
        "ez.md": "",
    },
    "two trailing slashes never match": {
        ".gitignore": "docs//\nother/\n",
        "docs/a.md": "",
        "other/a.md": "",
        "keep.md": "",
    },
    "question mark and class match one byte not one character": {
        ".gitignore": "note-?.md\nb-??.md\nc-*.md\n[\u00e9]x.md\n[\u00e9\u00e9]y.md\n",
        "note-\u00e9.md": "",
        "note-a.md": "",
        "b-\u00e9.md": "",
        "b-a.md": "",
        "c-\u00e9.md": "",
        "\u00e9x.md": "",
        "bx.md": "",
        "\u00e9y.md": "",
    },
    "range endpoint is the next raw character, even a bracket": {
        ".gitignore": "[0-[:digit:]].md\n",
        "5.md": "",
        "0].md": "",
        "0.md": "",
    },
    "posix class before a hyphen keeps the hyphen literal": {
        ".gitignore": "[[:digit:]-0].md\n",
        "5.md": "",
        "-.md": "",
        "a.md": "",
    },
    "unterminated bracket matches nothing": {
        ".gitignore": "[abc.md\nkeep[.md\n",
        "a.md": "",
        "[abc.md": "",
        "keep[.md": "",
    },
    "unknown posix class matches nothing": {
        ".gitignore": "[[:nonsense:]].md\n",
        "a.md": "",
        ":.md": "",
    },
    "an invalid utf-8 byte does not disable the other rules": {
        ".gitignore": b"caf\xff.md\nsecret.md\ndocs/\n",
        "secret.md": "",
        "docs/a.md": "",
        "keep.md": "",
        "caf.md": "",
    },
    "crlf line endings and a byte order mark": {
        ".gitignore": b"\xef\xbb\xbfone.md\r\ntwo.md\r\n",
        "one.md": "",
        "two.md": "",
        "three.md": "",
    },
    "pattern with an inner slash is anchored": {
        ".gitignore": "docs/private.md\n",
        "docs/private.md": "",
        "x/docs/private.md": "",
        "private.md": "",
    },
}


def _write_tree(root: Path, files: dict[str, str | bytes], ignore_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = rel
        if rel.endswith(".gitignore"):
            target = rel.removesuffix(".gitignore") + ignore_name
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)


def _git_ignored(root: Path, rels: list[str], *, init: bool = True) -> set[str]:
    if init:
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


_TOKENS = [
    "a",
    "b",
    "0",
    "-",
    ".",
    "é",
    "?",
    "*",
    "**",
    "/",
    "[",
    "]",
    "!",
    "^",
    ":",
    "[:digit:]",
    "[:alpha:]",
    "[:punct:]",
    "\\",
]
_LINE = st.tuples(
    st.sampled_from(["", "!", "/", "!/"]),
    st.lists(st.sampled_from(_TOKENS), min_size=1, max_size=7),
    st.sampled_from(["", "", "/"]),
).map(lambda parts: parts[0] + "".join(parts[1]) + parts[2])
_STEM = st.text(alphabet="ab0-.é[]!^:", min_size=1, max_size=4).filter(
    # A leading ":" would be read by check-ignore as pathspec magic.
    lambda stem: not stem.startswith((".", ":"))
)


@pytest.fixture(scope="module")
def property_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("property")
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_GIT_ENV)
    return root


@pytest.mark.timeout(180)
@settings(
    max_examples=150,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    root_lines=st.lists(_LINE, min_size=1, max_size=3),
    nested_lines=st.lists(_LINE, max_size=2),
    stems=st.lists(_STEM, min_size=1, max_size=4, unique=True),
)
def test_generated_patterns_agree_with_git(
    property_root: Path,
    root_lines: list[str],
    nested_lines: list[str],
    stems: list[str],
) -> None:
    files: dict[str, str | bytes] = {
        ".gitignore": "\n".join(root_lines) + "\n",
        "d/.gitignore": "\n".join(nested_lines) + "\n",
    }
    for stem in stems:
        for rel in (f"{stem}.md", f"d/{stem}.md", f"{stem}/in.md"):
            files[rel] = ""
    markdown = sorted(rel for rel in files if rel.endswith(".md"))
    # One repository for every example; each example's tree is a directory
    # inside it, so its ignore files anchor exactly as a repository root's do.
    git_case = Path(tempfile.mkdtemp(dir=property_root / "repo"))
    quarry_case = Path(tempfile.mkdtemp(dir=property_root))
    try:
        _write_tree(git_case, files, ".gitignore")
        expected = set(markdown) - _git_ignored(git_case, markdown, init=False)
        _write_tree(quarry_case, files, ".gitignore")
        discovery = FileDiscovery(quarry_case)
        scanned = {str(p.relative_to(quarry_case)) for p in discovery.discover(_EXTS)}
        live = {
            rel for rel in markdown if discovery.is_indexable(quarry_case / rel, _EXTS)
        }
    finally:
        shutil.rmtree(git_case, ignore_errors=True)
        shutil.rmtree(quarry_case, ignore_errors=True)
    context = (root_lines, nested_lines)
    assert scanned == expected, context
    assert live == expected, context
