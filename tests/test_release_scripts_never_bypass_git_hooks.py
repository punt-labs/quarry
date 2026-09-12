"""Regression test: release scripts must not bypass git hooks (pkit-hsyi)."""

from __future__ import annotations

import shlex
from pathlib import Path


def _stripped_code(path: Path) -> str:
    """Return *path*'s source with shell comments removed, quote-aware.

    A naive ``line.partition("#")`` treats any ``#`` as a comment
    delimiter, even one inside a quoted string — e.g. ``git commit -m
    "release (#123)" --no-verify`` would have the ``--no-verify`` half
    dropped as "comment", producing a false negative. ``shlex.split``
    with ``comments=True`` tokenizes each line respecting quotes, so a
    ``#`` only starts a comment outside quotes.

    Some lines (e.g. those opening a multi-line embedded Python string)
    do not parse standalone and raise ``ValueError``; those are kept
    verbatim rather than stripped. A parse failure must never hide real
    code, so failing to strip is the safe direction — the raw line goes
    into the scan intact.
    """
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            tokens = shlex.split(raw, comments=True)
        except ValueError:
            lines.append(raw)
            continue
        lines.append(" ".join(tokens))
    return "\n".join(lines)


def test_release_scripts_never_bypass_git_hooks() -> None:
    """No release-path script may pass ``--no-verify`` to git.

    The org CLAUDE.md bans ``--no-verify`` outright. ``release-plugin.sh``
    previously carried it on the "prepare plugin for release" commit; this
    test greps both release-path scripts so a reintroduction fails
    immediately, not on the next release. Comments in the target scripts
    are stripped before scanning, so prose describing the ban (e.g. in
    ``restore-dev-plugin.sh``'s CONTRACT comment) does not trigger a
    false positive.
    """
    root = Path(__file__).parent.parent
    targets = [
        root / "scripts" / "release-plugin.sh",
        root / "scripts" / "restore-dev-plugin.sh",
    ]
    for path in targets:
        code_only = _stripped_code(path)
        assert "--no-verify" not in code_only, (
            f"{path.name} reintroduced --no-verify — org CLAUDE.md bans "
            "the flag; let the hooks run or surface a real hook failure."
        )


class TestStrippedCode:
    """``_stripped_code`` must not mistake a quoted ``#`` for a comment."""

    def test_hash_inside_quotes_is_not_a_comment(self, tmp_path: Path) -> None:
        script = tmp_path / "script.sh"
        script.write_text('git commit -m "release (#123)" --no-verify\n')
        assert "--no-verify" in _stripped_code(script)

    def test_real_comment_is_stripped(self, tmp_path: Path) -> None:
        script = tmp_path / "script.sh"
        script.write_text("git commit -m foo # uses --no-verify, do not copy\n")
        assert "--no-verify" not in _stripped_code(script)

    def test_full_line_comment_is_stripped(self, tmp_path: Path) -> None:
        script = tmp_path / "script.sh"
        script.write_text("# --no-verify is banned org-wide\n")
        assert "--no-verify" not in _stripped_code(script)

    def test_unparseable_line_kept_verbatim(self, tmp_path: Path) -> None:
        """A line that can't stand alone (e.g. opens a multi-line quote)
        falls back to the raw line rather than being silently dropped."""
        script = tmp_path / "script.sh"
        script.write_text('python3 -c "\n--no-verify\n"\n')
        assert "--no-verify" in _stripped_code(script)
