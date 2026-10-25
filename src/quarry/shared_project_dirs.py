"""Claude Code projects dirs that more than one registration encodes to (#557)."""

from __future__ import annotations

import functools
import json
import logging
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.backfill_mapping import ProjectMapping

logger = logging.getLogger(__name__)

_HEAD_BYTES = 1 << 20  # attribution reads at most this much of a transcript


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
        """Return the first ``cwd`` in the head of *transcript*, or ``None``.

        An unreadable transcript is logged and, like one recording no ``cwd``,
        attributed to no project.
        """
        try:
            stat = transcript.stat()
            return SharedProjectDirs._first_cwd(
                transcript, stat.st_size, stat.st_mtime_ns
            )
        except OSError as exc:
            logger.warning(
                "backfill: a transcript in shared dir %s could not be read (%s); "
                "it is attributed to no project",
                transcript.parent.name,
                type(exc).__name__,
            )
            return None

    @staticmethod
    @functools.lru_cache(maxsize=4096)
    def _first_cwd(transcript: Path, size: int, mtime_ns: int) -> str | None:
        """Return the first string ``cwd`` among the records in *transcript*'s head.

        Only the first :data:`_HEAD_BYTES` are read, so a huge record cannot balloon
        the scan; a malformed or too-deeply-nested record is passed over.  Keyed on
        the transcript's size and mtime, so each sharing registration reuses one
        scan of an unchanged file.  A read error propagates and is not cached.
        """
        del size, mtime_ns
        with transcript.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
        for line in head.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            cwd = record.get("cwd") if isinstance(record, dict) else None
            if isinstance(cwd, str):
                return cwd
        return None
