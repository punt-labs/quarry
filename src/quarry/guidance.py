"""Own quarry's vendored repo user-guide and its ``@``-import contract.

The host ``CLAUDE.md`` is user-owned prose: the only mutation any tool may
make is to add or remove a single ``@``-import line pointing at a file the
tool owns entirely. See ``punt-kit/standards/tool-enable-disable.md`` (§ 2.1
host is user-owned, § 2.3 deposit + import, § 2.4 canonical import string).
No marker blocks, no fenced sections, no rendered copies of the guide in
any host file — the ``@``-import composes at read time.
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING, Self, final

from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["REPO_IMPORT_LINE", "Guidance"]

_GUIDE_RELATIVE = (".punt-labs", "quarry", "CLAUDE.md")
_GUIDE_RESOURCE = ("quarry.data", "repo-guide.md")

# The canonical repo import line (tool-enable-disable.md § 2.4): forward
# slashes, no ``./`` prefix, no trailing slash, one physical line.
REPO_IMPORT_LINE = "@.punt-labs/quarry/CLAUDE.md"


@final
class Guidance:
    """Own quarry's vendored repo user-guide file.

    :meth:`deposit` writes ``<repo>/.punt-labs/quarry/CLAUDE.md`` wholesale —
    the vendored zone (punt-labs-dir.md § 7), overwritten on every enable so the
    same tool version always produces identical output.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def guide_path(self) -> Path:
        """Return the deposited guide's path under the tool subtree."""
        return self._root.joinpath(*_GUIDE_RELATIVE)

    def deposit(self) -> None:
        """Write the vendored guide wholesale, following no symlink.

        Routes through :class:`quarry.safe_paths.SafeRepoPath` so a hostile repo
        cannot redirect the wholesale overwrite outside the repo by planting a
        symlink at ``.punt-labs`` or ``.punt-labs/quarry``: a symlinked ancestor
        is refused, and the write lands atomically on the real in-repo guide.
        """
        SafeRepoPath(self._root, _GUIDE_RELATIVE).write_atomic(
            self._guide_text(), mode=0o644
        )

    @staticmethod
    def _guide_text() -> str:
        package, resource = _GUIDE_RESOURCE
        try:
            return files(package).joinpath(resource).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            msg = (
                f"quarry vendored guide not found: expected {package}:{resource}. "
                "This is a packaging bug — the wheel must ship the data package."
            )
            raise RuntimeError(msg) from exc
