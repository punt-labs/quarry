"""Tests for quarry.background_ingest — detached ingest job spec."""

from __future__ import annotations

import sys
from pathlib import Path

from quarry.background_ingest import BackgroundIngestJob


def test_command_uses_current_interpreter_and_entry_point() -> None:
    """The argv runs the hook entry module with the same interpreter."""
    job = BackgroundIngestJob(
        document_name="session-abc",
        collection="repo",
        lancedb_path=Path("/data/lance"),
        session_prefix="abc12345",
    )
    argv = job.command(Path("/tmp/session-abc.txt"))
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "quarry._hook_entry", "ingest-background"]


def test_command_carries_all_job_fields_in_order() -> None:
    """Every field lands in the argv slot the subprocess reads it from."""
    job = BackgroundIngestJob(
        document_name="doc",
        collection="col",
        lancedb_path=Path("/db"),
        session_prefix="pre",
        agent_handle="rmh",
        memory_type="fact",
        summary="a note",
    )
    argv = job.command(Path("/tmp/f.txt"))
    assert argv[4:] == [
        "/tmp/f.txt",
        "doc",
        "col",
        "/db",
        "pre",
        "rmh",
        "fact",
        "a note",
    ]


def test_command_defaults_optional_tags_to_empty() -> None:
    """Unset agent-memory tags serialize as empty strings, not omitted args."""
    job = BackgroundIngestJob(
        document_name="doc",
        collection="col",
        lancedb_path=Path("/db"),
        session_prefix="pre",
    )
    argv = job.command(Path("/tmp/f.txt"))
    assert argv[-3:] == ["", "", ""]
