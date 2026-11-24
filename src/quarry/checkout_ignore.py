"""Verify that Git excludes paths from a checkout's index."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from pathlib import Path


@final
class CheckoutIgnore:
    """Use Git's own ignore rules without treating tracked files as ignored."""

    __slots__ = ()

    @classmethod
    def covers(
        cls, root: Path, paths: tuple[str, ...], *, include_parents: bool
    ) -> bool | None:
        """Check the target checkout, or its enclosing checkout for a capture write.

        For enablement, an unrelated parent checkout's ignore must not prevent
        writing a rule to a project that can later become its own checkout.
        Capture writes instead need protection in whichever checkout owns them.
        """
        if not cls._has_checkout(root, include_parents=include_parents):
            return None
        git = shutil.which("git")
        if git is None:
            return False
        targets = os.fsencode("\0".join((*paths, "")))
        result = subprocess.run(  # noqa: S603
            [git, "check-ignore", "-z", "--stdin"],
            cwd=root,
            capture_output=True,
            check=False,
            input=targets,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout == targets

    @classmethod
    def protect_future_checkout(cls, directory: int, child: str) -> None:
        """Exclude the captures directory before Git is initialized above it."""
        rules = os.fsencode(f"/{child}/\n/.gitignore\n")
        try:
            fd = os.open(
                ".gitignore",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            cls._verify_marker(directory, rules)
        else:
            with os.fdopen(fd, "wb") as marker:
                marker.write(rules)
                marker.flush()
                os.fsync(marker.fileno())

    @classmethod
    def _verify_marker(cls, directory: int, rules: bytes) -> None:
        """Do not follow or replace an existing ignore file."""
        fd = os.open(
            ".gitignore", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as marker:
            if marker.read(len(rules) + 1) != rules:
                msg = "cannot verify the captures directory's .gitignore"
                raise PermissionError(msg)

    @staticmethod
    def _has_checkout(root: Path, *, include_parents: bool) -> bool:
        """Recognize both Git directories and worktree pointer files."""
        candidates = (root, *root.parents) if include_parents else (root,)
        return any((path / ".git").exists() for path in candidates)
