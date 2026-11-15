"""The per-project ``config.md`` template and its exclusive-create writer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ProjectConfig"]

_TEMPLATE: Final = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
  session_end: true
  web_search: true
  read: false
  subagent_stop: true
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
- `session_end`: capture session transcripts on session close (fires even when
  a session never compacts)
- `web_search`: auto-ingest a digest of `WebSearch` results
- `read`: capture prose files (.md/.pdf/.docx/...) read from outside the tree —
  default `false` because `Read` fires often and has the highest secret-leak
  surface; opt in after confirming the filter set is clean
- `subagent_stop`: capture subagent transcripts on completion
- `shadow`: pre-create the private repo, then set `enabled: true`
"""


@final
class ProjectConfig:
    """The ``.punt-labs/quarry/config.md`` template, and where it writes.

    A single-method class rather than a bare module function + constant: the
    template text and the one operation on it (write itself into a repo) are
    one unit, and ``text`` lets a caller (the enable-report test suite) read
    the template without reaching for a private module constant.
    """

    __slots__ = ()

    @property
    def text(self) -> str:
        """Return the template's content."""
        return _TEMPLATE

    def write(self, directory: Path) -> str:
        """Write config.md exclusively, never overwriting or following a symlink.

        Returns the written path. Routes through
        :class:`quarry.safe_paths.SafeRepoPath` so a hostile repo
        cannot redirect the create outside the repo via a symlinked ``.punt-labs``
        ancestor or a symlinked ``config.md`` leaf. An existing regular config is
        left untouched (idempotent); a non-regular entry at the path is refused.
        """
        config = SafeRepoPath(directory, (".punt-labs", "quarry", "config.md"))
        config.create_exclusive(self.text, mode=0o644)
        return str(config.path)
