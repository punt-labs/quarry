"""File discovery for directory sync: walk, filter, hash."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Final, Self

import pathspec

logger = logging.getLogger(__name__)

_DEFAULT_IGNORE_PATTERNS: Final[list[str]] = [
    "__pycache__/",
    "*.pyc",
    "node_modules/",
    ".venv/",
    "venv/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".DS_Store",
]

_HASH_CHUNK_SIZE: Final[int] = 1 << 20  # 1 MiB


class FileDiscovery:
    """Discover indexable files under a directory, respecting ignore rules."""

    __slots__ = ("_directory", "_root_resolved")

    _directory: Path
    _root_resolved: Path | None

    def __new__(cls, directory: Path) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        try:
            self._root_resolved = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("Cannot resolve registered root: %s", directory)
            self._root_resolved = None
        return self

    @property
    def directory(self) -> Path:
        return self._directory

    def discover(self, extensions: frozenset[str]) -> list[Path]:
        """Recursively find files matching *extensions* under the directory.

        Respects ``.gitignore`` (at every level), ``.quarryignore``, and
        hardcoded ignore patterns (``venv/``, ``node_modules/``, etc.).
        Skips dotfiles, macOS resource forks (``._*``), and files inside
        hidden directories (``.Trash``, ``.git``, etc.).

        Symlinks whose target resolves outside the directory are dropped and
        logged as a warning.

        Returns absolute paths, sorted for deterministic order.
        """
        if self._root_resolved is None:
            return []

        root_spec = self.load_ignore_spec()
        result: list[Path] = []

        for dirpath_str, dirnames, filenames in os.walk(self._directory):
            dirpath = Path(dirpath_str)
            rel_dir = dirpath.relative_to(self._directory)
            local_spec = (
                self._read_local_ignore(dirpath) if dirpath != self._directory else None
            )

            # Prune hidden and ignored directories (in-place for os.walk)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".")
                and not root_spec.match_file(str(rel_dir / d) + "/")
                and (local_spec is None or not local_spec.match_file(d + "/"))
            )

            for filename in sorted(filenames):
                if filename.startswith((".", "._")):
                    continue
                filepath = dirpath / filename
                if filepath.suffix.lower() not in extensions:
                    continue
                rel_path = str(filepath.relative_to(self._directory))
                if root_spec.match_file(rel_path):
                    continue
                if local_spec is not None and local_spec.match_file(filename):
                    continue
                if filepath.is_symlink() and not self._symlink_inside_root(filepath):
                    continue
                result.append(filepath.absolute())

        return result

    @staticmethod
    def content_hash(path: Path) -> str:
        """Return a fast content hash of *path* for change detection.

        Uses ``blake2b`` with a 16-byte digest (128 bits).
        """
        h = hashlib.blake2b(digest_size=16)
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()

    def load_ignore_spec(self) -> pathspec.PathSpec:
        """Build a PathSpec from ``.gitignore``, ``.quarryignore``, and defaults."""
        lines: list[str] = list(_DEFAULT_IGNORE_PATTERNS)
        for name in (".gitignore", ".quarryignore"):
            ignore_path = self._directory / name
            if ignore_path.is_file():
                lines.extend(ignore_path.read_text(encoding="utf-8").splitlines())
        return pathspec.PathSpec.from_lines("gitignore", lines)

    @staticmethod
    def _read_local_ignore(dirpath: Path) -> pathspec.PathSpec | None:
        """Read ``.gitignore`` from *dirpath*, returning a PathSpec or None."""
        gitignore = dirpath / ".gitignore"
        if not gitignore.is_file():
            return None
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)

    def _symlink_inside_root(self, link: Path) -> bool:
        """Return True iff *link*'s target resolves inside the root."""
        if self._root_resolved is None:
            return False
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("Skipping unresolvable symlink: %s", link)
            return False
        try:
            target.relative_to(self._root_resolved)
        except ValueError:
            logger.warning(
                "Skipping symlink %s that escapes registered root: %s",
                link,
                target,
            )
            return False
        return True
