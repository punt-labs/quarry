"""Claude Code projects dirs that more than one registration encodes to (#557)."""

from __future__ import annotations

import contextlib
import json
import logging
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.backfill_mapping import ProjectMapping

logger = logging.getLogger(__name__)


@final
class SharedProjectDirs:
    """File a shared projects dir's transcripts by the ``cwd`` each records.

    Claude Code's directory encoding is lossy: ``/w/my.notes`` and
    ``/w/my_notes`` both become ``-w-my-notes``.  When two registrations map to
    one dir, its transcripts belong to whichever project each says it ran in; a
    transcript that records no ``cwd`` cannot be attributed and is skipped.
    """

    @staticmethod
    def mark(mappings: list[ProjectMapping]) -> list[ProjectMapping]:
        """Flag each mapping whose dir another mapping shares, warning once per dir."""
        counts = Counter(mapping.encoded_dir for mapping in mappings)
        for name in sorted(name for name, count in counts.items() if count > 1):
            logger.warning(
                "backfill: %s is shared by %d registrations; "
                "filing its transcripts by their recorded cwd",
                name,
                counts[name],
            )
        return [replace(m, shared=counts[m.encoded_dir] > 1) for m in mappings]

    @staticmethod
    def ran_in(transcripts: list[Path], project_path: str) -> list[Path]:
        """Return the *transcripts* that record *project_path* as their ``cwd``."""
        mine = Path(project_path)
        return [
            transcript
            for transcript in transcripts
            if (cwd := SharedProjectDirs.recorded_cwd(transcript)) and Path(cwd) == mine
        ]

    @staticmethod
    def recorded_cwd(transcript: Path) -> str | None:
        """Return the first ``cwd`` *transcript* records, or ``None``."""
        with contextlib.suppress(OSError), transcript.open(errors="replace") as lines:
            for line in lines:
                with contextlib.suppress(ValueError):
                    record = json.loads(line)
                    if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                        return str(record["cwd"])
        return None
