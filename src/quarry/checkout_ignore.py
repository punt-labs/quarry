"""Verify that Git excludes paths from a checkout's index."""

from __future__ import annotations

import errno
import os
import secrets
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
    def project_root(cls, directory: Path) -> Path:
        """Find the owning checkout for a session started in a subdirectory."""
        directory = directory.resolve()
        marker = cls._find_marker(directory, include_parents=True)
        if marker is not None and (marker.is_file() or (marker / "HEAD").is_file()):
            return marker.parent
        return directory

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
            cls._verify_marker(directory, rules)
        except FileNotFoundError:
            cls._publish_marker(directory, rules)

    @classmethod
    def _publish_marker(cls, directory: int, rules: bytes) -> None:
        """Publish a completed marker without replacing an existing rule file."""
        temp = f".gitignore.quarry-{secrets.token_hex(8)}"
        fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
        )
        try:
            cls._sync_marker(fd, rules)
            try:
                os.link(temp, ".gitignore", src_dir_fd=directory, dst_dir_fd=directory)
            except FileExistsError:
                cls._verify_marker(directory, rules)
        finally:
            os.unlink(temp, dir_fd=directory)

    @staticmethod
    def _sync_marker(fd: int, rules: bytes) -> None:
        """Write the complete small rule file or close it and fail."""
        try:
            if os.write(fd, rules) != len(rules):
                msg = "incomplete write of capture ignore marker"
                raise OSError(errno.EIO, msg)
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _verify_marker(cls, directory: int, rules: bytes) -> None:
        """Do not follow or replace an existing ignore file."""
        fd = os.open(
            ".gitignore", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            if os.read(fd, len(rules) + 1) != rules:
                msg = "cannot verify the captures directory's .gitignore"
                raise PermissionError(msg)
        finally:
            os.close(fd)

    @classmethod
    def _has_checkout(cls, root: Path, *, include_parents: bool) -> bool:
        return cls._find_marker(root, include_parents=include_parents) is not None

    @staticmethod
    def _find_marker(root: Path, *, include_parents: bool) -> Path | None:
        """Recognize both Git directories and worktree pointer files."""
        candidates = (root, *root.parents) if include_parents else (root,)
        markers = (path / ".git" for path in candidates)
        return next((marker for marker in markers if marker.exists()), None)
