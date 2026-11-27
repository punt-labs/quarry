"""Remove a project's config.md; a directory that cannot be searched reads as absent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Self, final

from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ConfigDiscard"]

_RELATIVE: Final = (".punt-labs", "quarry", "config.md")


@final
class ConfigDiscard:
    """Unlink one directory's config, telling an unsearchable path from a failed unlink.

    A directory on the path that cannot be searched holds no config this
    process can see, so there is nothing to remove.  A config that is still
    there after its unlink failed (read-only directory, immutable file,
    read-only filesystem) is not absent: the caller must hear about it.
    """

    __slots__ = ("_config",)

    _config: SafeRepoPath

    def __new__(cls, directory: Path) -> Self:
        """Bind the removal to *directory*'s ``config.md``."""
        self = super().__new__(cls)
        self._config = self.handle(directory)
        return self

    @staticmethod
    def handle(directory: Path) -> SafeRepoPath:
        """Return the symlink-safe handle on *directory*'s ``config.md``."""
        return SafeRepoPath(directory, _RELATIVE)

    def remove(self) -> bool:
        """Unlink the config if it is a regular file; return whether it was removed.

        Raises ``ValueError`` when an ancestor is a symlink or cannot be
        entered: the walk refuses both alike, so an unsearchable directory on
        the path usually surfaces here.  Returns ``False`` when only the
        ``lstat`` of the leaf is denied (the parent opens but cannot be
        searched).  An ``OSError`` from a config that is still a regular file
        (failed unlink) propagates.
        """
        try:
            return self._config.remove()
        except OSError:
            if self._config.is_regular_file():
                raise
            return False
