"""Judge a project's ``config.md`` by what the capture hooks will do with it."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from quarry._frontmatter import Frontmatter
from quarry._stdlib import HookConfig
from quarry.config_discard import ConfigDiscard

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ConfigVerdict"]


class ConfigVerdict(Enum):
    """What a directory's ``config.md`` amounts to.

    ``DEFAULT`` means every hook setting equals the hook default and no
    ``shadow:`` block is uncommented, whatever the file's age, line endings or
    comments: deleting it changes nothing the hooks do.
    """

    ABSENT = "absent"
    DEFAULT = "default"
    CUSTOMISED = "customised"
    UNREADABLE = "unreadable"
    LINKED = "linked"

    @property
    def left_in_place(self) -> bool:
        """Return whether a covered directory's disable must not remove this config."""
        return self in {
            ConfigVerdict.CUSTOMISED,
            ConfigVerdict.UNREADABLE,
            ConfigVerdict.LINKED,
        }

    @classmethod
    def of(cls, directory: Path) -> ConfigVerdict:
        """Return the verdict on ``directory``'s config, never raising.

        A symlinked ``config.md`` or ``.punt-labs`` is ``LINKED`` when it leads
        to a file: the hook follows it, this tool never does.  Text that cannot
        be read or decoded is ``UNREADABLE``: the hook falls back to defaults
        for it, so it is neither applied nor ours to delete.
        """
        config = ConfigDiscard.handle(directory)
        if not config.is_regular_file():
            return cls.LINKED if cls._leads_to_file(config.path) else cls.ABSENT
        try:
            text = config.read_text()
        except (OSError, ValueError):
            return cls.UNREADABLE
        return cls.CUSTOMISED if cls._changes_behaviour(text) else cls.DEFAULT

    @staticmethod
    def _leads_to_file(path: Path) -> bool:
        """Return whether *path* resolves to a file; an unsearchable ancestor is no."""
        try:
            return path.is_file()
        except OSError:
            return False

    @staticmethod
    def discard(directory: Path) -> bool:
        """Remove ``directory``'s config if it is a regular file; return whether it was.

        Raises ``ValueError`` when an ancestor is a symlink or cannot be entered
        (an unsearchable directory on the path usually surfaces this way), and
        ``OSError`` when a config that is still a regular file cannot be
        unlinked.  See :class:`~quarry.config_discard.ConfigDiscard`.
        """
        return ConfigDiscard(directory).remove()

    @staticmethod
    def _changes_behaviour(text: str) -> bool:
        """Return whether the hooks act differently for *text* than with no file."""
        return (
            HookConfig.parse(text) != HookConfig()
            or Frontmatter(text).block("shadow") is not None
        )
