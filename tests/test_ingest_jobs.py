"""Tests for quarry.daemon.ingest_jobs — the URL job's spool_record contract."""

from __future__ import annotations

from quarry.daemon.ingest_jobs import IngestJob


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
