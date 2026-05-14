"""Tests for quarry._hook_entry dataclasses."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry._hook_entry import IngestJob


class TestIngestJob:
    def test_construction_required_fields(self) -> None:
        job = IngestJob(
            text_file=Path("/tmp/transcript.txt"),
            document_name="session-abc12345-20260513T120000",
            collection="my-captures",
            lancedb_path="/home/user/.punt-labs/quarry/lancedb",
            session_prefix="abc12345",
        )
        assert job.text_file == Path("/tmp/transcript.txt")
        assert job.document_name == "session-abc12345-20260513T120000"
        assert job.collection == "my-captures"
        assert job.lancedb_path == "/home/user/.punt-labs/quarry/lancedb"
        assert job.session_prefix == "abc12345"
        assert job.agent_handle is None
        assert job.memory_type is None
        assert job.summary is None

    def test_construction_all_fields(self) -> None:
        job = IngestJob(
            text_file=Path("/tmp/t.txt"),
            document_name="doc",
            collection="col",
            lancedb_path="/db",
            session_prefix="abcd1234",
            agent_handle="rmh",
            memory_type="fact",
            summary="a useful fact",
        )
        assert job.agent_handle == "rmh"
        assert job.memory_type == "fact"
        assert job.summary == "a useful fact"

    def test_frozen(self) -> None:
        job = IngestJob(
            text_file=Path("/tmp/t.txt"),
            document_name="doc",
            collection="col",
            lancedb_path="/db",
            session_prefix="x",
        )
        with pytest.raises(AttributeError):
            job.collection = "other"  # type: ignore[misc]
