"""Tests for the ``quarry.api`` wire-contract package.

These cover the two properties the contract must hold: the models round-trip
the daemon's current field shapes exactly (bug-class-3 parity), and the package
imports with **no** engine dependency (so a pure client stays light).
"""

from __future__ import annotations

import subprocess
import sys

import quarry.api as api


def test_all_exports_are_importable() -> None:
    """Every name in ``__all__`` resolves to a real attribute."""
    for name in api.__all__:
        assert hasattr(api, name), name


def test_api_version_is_major_string() -> None:
    assert api.API_VERSION == "1"


def test_search_request_reads_q_alias() -> None:
    """The search text binds from the wire param ``q``."""
    req = api.SearchRequest.model_validate({"q": "hello", "limit": 5})
    assert req.query == "hello"
    assert req.limit == 5


def test_search_request_builds_by_field_name_and_serializes_to_alias() -> None:
    """A client may construct by field name; the wire form keeps the ``q`` alias."""
    req = api.SearchRequest(query="hello")
    assert req.query == "hello"
    assert req.model_dump(by_alias=True)["q"] == "hello"


def test_search_hit_preserves_unknown_fields() -> None:
    """A new engine column passes through untouched (field parity)."""
    row = {
        "document_name": "a.md",
        "collection": "default",
        "page_number": 1,
        "chunk_index": 0,
        "text": "hi",
        "page_type": "text",
        "source_format": ".md",
        "agent_handle": "",
        "memory_type": "",
        "summary": "",
        "similarity": 0.9,
        "future_field": "kept",
    }
    hit = api.SearchHit.model_validate(row)
    assert hit.model_dump()["future_field"] == "kept"


def test_document_info_preserves_unknown_fields() -> None:
    row = {
        "document_name": "a.md",
        "document_path": "/x/a.md",
        "collection": "default",
        "total_pages": 1,
        "chunk_count": 2,
        "indexed_pages": 1,
        "ingestion_timestamp": "2026-07-14",
        "extra": 1,
    }
    assert api.DocumentInfo.model_validate(row).model_dump()["extra"] == 1


def test_task_status_omits_absent_fields() -> None:
    """A running task serializes without ``results``/``error`` (exclude_none)."""
    running = api.TaskStatus(task_id="sync-1", status="running")
    assert running.model_dump(exclude_none=True) == {
        "task_id": "sync-1",
        "status": "running",
    }


def test_error_body_allows_conflict_extras() -> None:
    """The 409 sync conflict adds status/task_id to the error envelope."""
    body = api.ErrorBody.model_validate(
        {"error": "Sync already in progress", "status": "running", "task_id": "sync-1"}
    )
    dumped = body.model_dump()
    assert dumped["error"] == "Sync already in progress"
    assert dumped["task_id"] == "sync-1"


def test_deregister_accepted_carries_removed() -> None:
    acc = api.DeregisterAccepted(task_id="deregister-1", removed=3)
    assert acc.status == "accepted"
    assert acc.removed == 3


def test_api_package_imports_without_engine() -> None:
    """``quarry.api`` must import with no lancedb/onnxruntime pulled in.

    Runs in a fresh interpreter so the main test process (which has the engine
    loaded) cannot mask a hidden import.
    """
    code = (
        "import sys, quarry.api;"
        "assert 'lancedb' not in sys.modules, "
        "sorted(m for m in sys.modules if 'lance' in m);"
        "assert 'onnxruntime' not in sys.modules;"
        "assert 'pyarrow' not in sys.modules;"
        "print('engine-free')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "engine-free" in result.stdout
