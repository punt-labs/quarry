"""A registered root as the walk and the live checks see it."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Self, final

from quarry.scratch_paths import ScratchGuard

logger = logging.getLogger(__name__)


@final
class RegisteredRoot:
    """The root directory, its real path, and where its walk may not go.

    A bulk walk never enters a symlinked directory, and it keeps a symlinked
    file only when the file's target lies inside the real root.  The live
    per-path checks ask this class so they draw the same line.
    """

    __slots__ = ("_directory", "_excluded", "_guard", "_resolved")

    _directory: Path
    _guard: ScratchGuard
    _resolved: Path | None
    _excluded: bool

    def __new__(cls, directory: Path) -> Self:
        """Resolve *directory*, recording a resolution failure."""
        self = super().__new__(cls)
        self._directory = directory
        self._guard = ScratchGuard()
        try:
            self._resolved = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("Cannot resolve registered root: %s", directory)
            self._resolved = None
        # A temp/scratch root (OS temp like ``/private/tmp`` OR a repo's own
        # ``.tmp``) is never a document tree: watching it OCR-storms everything
        # dropped there.  Refused up front so both the bulk scan and the live
        # path treat it as empty.
        self._excluded = self._resolved is not None and self._guard.refuses_root(
            self._resolved
        )
        if self._excluded:
            logger.debug("Skipping scratch/temp root: %s", self._resolved)
        return self

    @property
    def directory(self) -> Path:
        """The registered root as given."""
        return self._directory

    @property
    def guard(self) -> ScratchGuard:
        """The scratch guard that refused (or accepted) this root."""
        return self._guard

    @property
    def available(self) -> bool:
        """Whether the root resolved and is not a refused scratch root."""
        return self._resolved is not None and not self._excluded

    def relative(self, path: Path) -> Path:
        """Return *path* relative to the root; raise ``ValueError`` if outside."""
        return path.relative_to(self._directory)

    def hidden_or_skipped(self, rel: Path) -> bool:
        """Whether any segment of *rel* is hidden or a ScratchGuard skip-name.

        The lexical, no-disk-I/O check applied to a path whose file is
        already gone.
        """
        if any(part.startswith((".", "._")) for part in rel.parts):
            return True
        return self._guard.skips_below_root(rel)

    def crosses_symlink(self, parts: tuple[str, ...]) -> bool:
        """Whether any directory named by the root-relative *parts* is a symlink."""
        current = self._directory
        for name in parts:
            current /= name
            if current.is_symlink():
                return True
        return False

    def target_parts(self, path: Path) -> tuple[str, ...]:
        """Return the root-relative segments of *path*'s resolved target.

        Raises ``ValueError`` when the root or the target cannot be resolved,
        or the target lies outside the real root.
        """
        root = self._resolved
        if root is None:
            msg = f"root is not resolvable for {path}"
            raise ValueError(msg)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            msg = f"cannot resolve {path}"
            raise ValueError(msg) from exc
        try:
            return resolved.relative_to(root).parts
        except ValueError as exc:
            msg = f"{path} resolves outside the root: {resolved}"
            raise ValueError(msg) from exc
