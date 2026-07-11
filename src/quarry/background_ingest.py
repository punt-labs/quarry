"""Specification for a detached transcript-ingestion subprocess."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BackgroundIngestJob:
    """The parameters a detached ``ingest-background`` subprocess consumes.

    Bundles the argv-bound fields — document name, collection, database path,
    session prefix, and optional agent-memory tags — so the spawn site passes
    one job object instead of a nine-argument call.
    """

    document_name: str
    collection: str
    lancedb_path: Path
    session_prefix: str
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""

    def command(self, text_file: Path) -> list[str]:
        """Return the argv that ingests *text_file* in a detached process.

        Uses ``sys.executable`` rather than a PATH lookup so the child runs the
        same interpreter as the hook, avoiding PATH-trust issues.
        """
        return [
            sys.executable,
            "-m",
            "quarry._hook_entry",
            "ingest-background",
            str(text_file),
            self.document_name,
            self.collection,
            str(self.lancedb_path),
            self.session_prefix,
            self.agent_handle,
            self.memory_type,
            self.summary,
        ]
