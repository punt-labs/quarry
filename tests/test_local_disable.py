"""Tests for the local-file steps of a disable: config disposal and the guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.enabled_marker import EnabledMarker
from quarry.guidance import REPO_IMPORT_LINE
from quarry.local_disable import ConfigDisposal, GuardedDisable
from quarry.project_config import ProjectConfig


def _write(root: Path, text: str) -> Path:
    path = root / ".punt-labs" / "quarry" / "config.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_covered_template_config_is_removed(tmp_path: Path) -> None:
    path = _write(tmp_path, ProjectConfig().text)
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=True, kept=False
    )
    assert not path.exists()


def test_covered_customised_config_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "---\nauto_capture:\n  session_end: false\n---\n")
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=False, kept=True, why="customised"
    )
    assert path.exists()


def test_covered_unreadable_config_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    path.write_bytes(b"\xff\xfe\x00bad")
    assert ConfigDisposal.settle(tmp_path, covered=True).kept is True
    assert path.exists()


# The template as shipped before session_end/web_search/read/subagent_stop
# (commit 44b54f4) and before the commented shadow block (commit dafe412).
_TEMPLATE_BEFORE_HOOKS = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
# shadow:                          # push redacted captures to a PRIVATE shadow repo
#   enabled: false                 # opt-in network+security action, off by default
#   remote: ""                     # empty -> derive <origin>-quarry from origin
#   acknowledge_unverified: false  # push even when gh cannot confirm private
---

# Quarry Project Configuration

Controls quarry's passive knowledge capture. Set any field to `false` to disable
that capture type; uncomment `shadow` to move redacted captures off the public
repo into a per-project private shadow (`<repo>` -> `<repo>-quarry`).

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
- `shadow`: pre-create the private repo, then set `enabled: true`
"""

_TEMPLATE_BEFORE_SHADOW = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
---

# Quarry Project Configuration

This file controls quarry's passive knowledge capture for this project.
Set any field to `false` to disable that capture type.

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
"""


@pytest.mark.parametrize(
    "text",
    [
        _TEMPLATE_BEFORE_HOOKS,
        _TEMPLATE_BEFORE_SHADOW,
        ProjectConfig().text.replace("\n", "\r\n"),
        _TEMPLATE_BEFORE_HOOKS.replace("\n", "\r\n"),
    ],
    ids=["before-hooks", "before-shadow", "crlf-current", "crlf-before-hooks"],
)
def test_covered_untouched_config_of_any_age_is_removed(
    tmp_path: Path, text: str
) -> None:
    path = _write(tmp_path, "")
    path.write_bytes(text.encode())
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=True, kept=False
    )
    assert not path.exists()


@pytest.mark.parametrize(
    "text",
    [
        ProjectConfig().text.replace("read: false", "read: true"),
        ProjectConfig().text.replace("compaction: true", "compaction: off"),
        ProjectConfig().text.replace("web_search: true", "web_search: maybe"),
        _TEMPLATE_BEFORE_HOOKS.replace("web_fetch: true", "web_fetch: false"),
        ProjectConfig().text.replace(
            "# shadow:                          # push redacted captures to a PRIVATE"
            " shadow repo\n#   enabled: false",
            "shadow:\n  enabled: true",
        ),
    ],
    ids=["read-on", "compaction-off", "unparseable-value", "old-opt-out", "shadow-on"],
)
def test_covered_config_with_a_changed_meaning_is_kept(
    tmp_path: Path, text: str
) -> None:
    path = _write(tmp_path, text)
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=False, kept=True, why="customised"
    )
    assert path.exists()


def test_covered_unreadable_config_is_kept_and_marked_unreadable(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "")
    path.write_bytes(b"\xff\xfe\x00bad")
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=False, kept=True, why="unreadable"
    )


def test_covered_symlinked_config_is_left_and_marked_linked(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("---\nauto_capture:\n  session_end: false\n---\n")
    link = _write(tmp_path, "")
    link.unlink()
    link.symlink_to(real)
    assert ConfigDisposal.settle(tmp_path, covered=True) == ConfigDisposal(
        removed=False, kept=True, why="linked"
    )
    assert link.is_symlink()
    assert real.exists()


def test_covered_symlinked_ancestor_is_left_and_marked_linked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, ProjectConfig().text)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".punt-labs").symlink_to(outside / ".punt-labs")
    assert ConfigDisposal.settle(repo, covered=True) == ConfigDisposal(
        removed=False, kept=True, why="linked"
    )
    assert (outside / ".punt-labs" / "quarry" / "config.md").exists()


def test_uncovered_symlinked_config_is_as_before(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("x")
    link = _write(tmp_path, "")
    link.unlink()
    link.symlink_to(real)
    assert ConfigDisposal.settle(tmp_path, covered=False) == ConfigDisposal(
        removed=False, kept=False
    )


def test_uncovered_customised_config_is_removed(tmp_path: Path) -> None:
    path = _write(tmp_path, "---\nauto_capture:\n  session_end: false\n---\n")
    assert ConfigDisposal.settle(tmp_path, covered=False) == ConfigDisposal(
        removed=True, kept=False
    )
    assert not path.exists()


def test_absent_config_is_neither_removed_nor_kept(tmp_path: Path) -> None:
    for covered in (True, False):
        assert ConfigDisposal.settle(tmp_path, covered=covered) == ConfigDisposal(
            removed=False, kept=False
        )


def test_symlinked_ancestor_counts_as_absent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "x")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".punt-labs").symlink_to(outside / ".punt-labs")
    assert ConfigDisposal.settle(repo, covered=False) == ConfigDisposal(
        removed=False, kept=False
    )
    assert (outside / ".punt-labs" / "quarry" / "config.md").exists()


def test_guard_empty_repo_has_nothing_to_remove(tmp_path: Path) -> None:
    assert GuardedDisable(tmp_path).present() is False


def test_claude_md_without_the_import_has_nothing(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    assert GuardedDisable(tmp_path).present() is False


def test_marker_alone_counts(tmp_path: Path) -> None:
    EnabledMarker(tmp_path).write()
    assert GuardedDisable(tmp_path).present() is True


def test_import_alone_counts(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(f"{REPO_IMPORT_LINE}\n")
    assert GuardedDisable(tmp_path).present() is True


def test_unreadable_claude_md_errs_toward_present(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe\x00")
    assert GuardedDisable(tmp_path).present() is True
