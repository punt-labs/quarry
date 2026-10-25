"""Single choke point for writing redacted session capture files.

Both the PreCompact hook and the backfill path route capture writes through
:class:`CaptureWriter`, so scrubbing and the on-disk layout live in exactly one
place.  Scrubbing runs to completion *before* any bytes reach disk
(all-or-nothing): a scrub failure writes no file, and the on-disk write is
atomic, so a partial or half-redacted capture is never left behind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from quarry.atomic_file import AtomicFile
from quarry.scrub import scrub_and_log

if TYPE_CHECKING:
    from quarry.artifacts import SessionArtifacts

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """A single capture-file write request.

    ``label`` names the producer (``"pre-compact"`` or ``"backfill"``) for the
    scrub log line; it does not affect the written bytes, so the same request
    from either producer yields a byte-identical file.
    """

    project_dir: Path
    session_id: str
    timestamp: str
    artifacts: SessionArtifacts
    text: str
    label: str

    def capture_path(self) -> Path:
        """Return the capture file's path under the project's captures dir."""
        captures_dir = self.project_dir / ".punt-labs" / "quarry" / "captures"
        return captures_dir / f"session-{self.session_id[:8]}.md"

    def scrubbed_content(self) -> str:
        """Return the scrubbed frontmatter+body, or ``""`` if no frontmatter."""
        frontmatter = self.artifacts.format_frontmatter(self.session_id, self.timestamp)
        if not frontmatter:
            return ""
        return scrub_and_log(frontmatter + "\n\n" + self.text, self.label)


class CaptureWriter:
    """Scrub a :class:`CaptureRequest` and write it atomically to disk."""

    __slots__ = ()

    def write(self, request: CaptureRequest) -> bool:
        """Scrub *request* and write the capture file; return whether one landed.

        Fails silently — a capture-file problem must never block the main
        ingest flow.  Scrubbing precedes the write, and the write is atomic,
        so no partial or unredacted file is ever left on disk.
        """
        try:
            content = request.scrubbed_content()
            if content:
                AtomicFile(request.capture_path()).replace(content)
            return bool(content)
        except Exception:
            logger.exception("%s: capture file write failed", request.label)
            return False
