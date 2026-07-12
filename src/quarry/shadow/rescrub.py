"""Commit-time re-scrub of staged captures plus an I/O-race / write guard.

``CaptureReScrubber`` re-applies the DES-036 :class:`~quarry.scrub.Scrubber` to
every ``session-*.md`` in the captures dir before a commit, so a pre-fpc5 file
(written before write-time redaction existed) is redacted before it can enter a
commit.  Because the scrubber is idempotent, an already-redacted file is a
byte-for-byte no-op.

``verify_staged_clean`` is an I/O-race / write-failure guard, NOT an independent
PII oracle.  It asserts the git-STAGED bytes are a ``scrub`` fixed point, so it
catches a write race or a silent re-stage failure between stage and commit.  It
shares the scrubber's exact rules, so it can never catch a scrubber blind spot —
the residual PII classes (§4.5 of the design) are backstopped solely by the
private remote, not by this guard.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.scrub import Scrubber

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_CAPTURE_GLOB = "session-*.md"


@final
class CaptureReScrubber:
    """Re-scrub and fixed-point-verify the capture files in one directory."""

    __slots__ = ("_captures_dir", "_scrubber")

    _captures_dir: Path
    _scrubber: Scrubber

    def __new__(cls, captures_dir: Path) -> Self:
        self = super().__new__(cls)
        self._captures_dir = captures_dir
        self._scrubber = Scrubber()
        return self

    def rescrub_all(self) -> int:
        """Re-scrub every capture file, rewriting changed ones. Return the count.

        A write failure propagates (fail-closed): the caller aborts the commit
        rather than shipping a file whose scrubbed bytes never reached disk.
        """
        changed = 0
        for path in self._capture_files():
            original = path.read_text(encoding="utf-8")
            scrubbed = self._scrubber.scrub(original)[0]
            if scrubbed != original:
                self._atomic_write(path, scrubbed)
                changed += 1
        return changed

    def verify_staged_clean(self, staged: Mapping[str, str]) -> list[Path]:
        """Return staged captures whose blob is not a ``scrub`` fixed point.

        *staged* maps each staged ``session-*.md`` relpath to the blob text the
        commit will ship (read from the git INDEX, not the working tree).  A
        non-empty result means a write/re-stage race left un-rescrubbed bytes in
        the index; the caller aborts before the commit.  Verifying the staged
        blob — rather than the on-disk file — closes the gap where a silent
        re-stage failure leaves the index unscrubbed while the disk reads clean.
        This is an I/O-correctness guard, not proof of "no PII".
        """
        return [
            Path(rel)
            for rel, text in staged.items()
            if self._scrubber.scrub(text)[0] != text
        ]

    def _capture_files(self) -> list[Path]:
        if not self._captures_dir.is_dir():
            return []
        return sorted(self._captures_dir.glob(_CAPTURE_GLOB))

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* via a temp file and atomic rename, cleaning up on error."""
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
