"""One capture file on disk: scrubbed, owner-only, never through a symlink, durable."""

from __future__ import annotations

import logging
import os
import stat
from typing import TYPE_CHECKING, Self, final

from quarry.capture_directory import CaptureDirectory, CaptureSymlinkError
from quarry.capture_outcome import CaptureOutcome
from quarry.capture_temp import CaptureTemp
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.capture import CaptureRequest

logger = logging.getLogger(__name__)


@final
class CaptureFile:
    """Write a request's capture where only its owner can read it, and nowhere else.

    Mode 0600 from the first byte (:class:`~quarry.capture_temp.CaptureTemp`); a
    symlink anywhere in the path, even one planted mid-write, is refused or
    replaced, never followed (every step goes through a descriptor opened by
    :class:`~quarry.capture_directory.CaptureDirectory`); the directory is synced
    after the rename, and a failed sync is a warning.  Any other failure is
    logged by class and frames and is the outcome ``FAILED``: a capture-file
    problem never blocks the ingest it belongs to.
    """

    __slots__ = ("_request",)

    _request: CaptureRequest

    def __new__(cls, request: CaptureRequest) -> Self:
        self = super().__new__(cls)
        self._request = request
        return self

    def landed(self) -> bool:
        """Write the capture file; return whether one landed."""
        return self.write() is CaptureOutcome.WRITTEN

    def write(self) -> CaptureOutcome:
        """Scrub the request and write its capture file; say how that ended."""
        try:
            return self._write(self._request.scrubbed_content())
        except Exception as exc:
            logger.error(
                "%s: capture file write failed",
                self._request.label,
                exc_info=RedactedFailure.of(exc),
            )
            return CaptureOutcome.FAILED

    def _write(self, content: str) -> CaptureOutcome:
        """Write *content* to the capture file; nothing to write is an outcome."""
        if not content:
            return CaptureOutcome.EMPTY
        name = self._request.capture_path().name
        try:
            with CaptureDirectory(self._request.project_dir).opened(create=True) as fd:
                return self._store(fd, name, content)
        except CaptureSymlinkError:
            logger.warning("capture: a symlink in the path of %s; not written", name)
            return CaptureOutcome.SYMLINK

    @staticmethod
    def _store(directory: int, name: str, content: str) -> CaptureOutcome:
        """Refuse a symlink at the target, else write through a temp and sync."""
        try:
            mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
        except FileNotFoundError:
            mode = 0
        if stat.S_ISLNK(mode):
            logger.warning("capture: %s is a symlink; not written", name)
            return CaptureOutcome.SYMLINK
        CaptureTemp(directory, name).commit(content)
        CaptureFile._sync(directory)
        return CaptureOutcome.WRITTEN

    @staticmethod
    def _sync(directory: int) -> None:
        """Flush the directory entry the rename wrote; a failure is only a warning."""
        try:
            os.fsync(directory)
        except OSError as exc:
            logger.warning(
                "capture: could not sync the captures directory: %s",
                FailureReason.os_name(exc),
            )
