"""Tests for quarry.daemon.ingest_jobs — the spool_record drain-recovery contract."""

from __future__ import annotations

from quarry.daemon.ingest_jobs import CaptureIngestJob, IngestJob, ScrubbedIngestJob


def _remember(content: str, *, collection: str = "mem") -> ScrubbedIngestJob:
    return ScrubbedIngestJob(
        name="note",
        content=content,
        collection=collection,
        format_hint="auto",
        overwrite=True,
        scrub_label="remember",
        agent_handle="",
        memory_type="",
        summary="",
    )


def test_remember_spool_record_scrubs_its_content() -> None:
    """A remember snapshot is scrubbed, so a spooled file holds no raw secrets."""
    record = _remember("reach me at user@example.com").spool_record()
    assert record is not None
    assert record.kind == "remember"
    assert record.collection == "mem"
    assert "user@example.com" not in record.payload
    assert "[REDACTED:email]" in record.payload


def test_ingest_spool_record_carries_the_source_url() -> None:
    """An ingest snapshot is the source URL — the recoverable retry unit."""
    job = IngestJob(
        source="https://x.test/a",
        overwrite=False,
        collection="x.test",
        scrub=False,
        agent_handle="",
        memory_type="",
        summary="",
    )
    record = job.spool_record()
    assert record is not None
    assert record.kind == "ingest"
    assert record.payload == "https://x.test/a"


def test_capture_spool_record_is_none() -> None:
    """A capture has a durable transcript .md, so it is not spooled."""
    inline = _remember("c", collection="repo-captures")
    job = CaptureIngestJob(inline=inline, source_url="")
    assert job.spool_record() is None
