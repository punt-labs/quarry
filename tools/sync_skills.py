"""Keep ``src/quarry/_skills/`` byte-identical to ``plugin/skills/``.

``plugin/skills/`` is the canonical tree at repo root — Claude Code's
marketplace plugin loader reads skills from exactly that location, and that
cannot move. ``src/quarry/_skills/`` is a committed copy of the same content
under the package, so it ships inside the wheel: data under ``src/quarry/``
already ships automatically with ``module-name = "quarry"`` (``py.typed`` and
``data/repo-guide.md`` are proof; no ``[tool.uv.build-backend]`` change is
needed). ``make sync-skills`` regenerates the copy; ``make check-skills-sync``
(``--check`` here) fails if the two trees have drifted, so an edit to
``plugin/skills/`` that forgot to re-sync is caught at the gate, not
discovered later in a shipped wheel.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Self, final

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCE = _REPO_ROOT / "plugin" / "skills"
_PACKAGED = _REPO_ROOT / "src" / "quarry" / "_skills"


@final
class SkillsSync:
    """Copy, or compare, ``plugin/skills/`` against its packaged mirror."""

    __slots__ = ("_packaged", "_source")

    _source: Path
    _packaged: Path

    def __new__(cls, source: Path, packaged: Path) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._packaged = packaged
        return self

    def sync(self) -> None:
        """Replace the packaged mirror with a fresh copy of the source tree."""
        if self._packaged.exists():
            shutil.rmtree(self._packaged)
        shutil.copytree(self._source, self._packaged)

    def is_current(self) -> bool:
        """Return whether the packaged mirror's content hash matches the source's.

        A content hash, not ``filecmp.dircmp``'s default shallow comparison:
        ``dircmp`` trusts an ``os.stat`` signature (size + mtime) and skips
        reading bytes when it matches, which would call two files "same" after
        an edit that happened to preserve size — exactly the drift this check
        exists to catch.
        """
        return self._packaged.is_dir() and (
            self._tree_hash(self._source) == self._tree_hash(self._packaged)
        )

    @staticmethod
    def _tree_hash(root: Path) -> str:
        """Return an order-independent content hash of every file under *root*.

        A symlink is hashed by its link-target string, never dereferenced —
        matching :meth:`quarry.skills_source.SkillSource._hash_tree`, whose
        same discipline this mirrors (the skill trees carry no symlinks today,
        but a future one must still hash as what it IS, not what it points at).
        """
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(relative.encode())
                digest.update(path.readlink().as_posix().encode())
            elif path.is_file():
                digest.update(relative.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()


def main(argv: list[str]) -> int:
    """Sync ``src/quarry/_skills/`` from ``plugin/skills/``, or ``--check`` it."""
    sync = SkillsSync(_SOURCE, _PACKAGED)
    if "--check" in argv:
        if sync.is_current():
            return 0
        print(
            "src/quarry/_skills/ is stale vs plugin/skills/ — run `make sync-skills`",
            file=sys.stderr,
        )
        return 1
    sync.sync()
    print(f"synced {_PACKAGED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
