"""``StatusReport`` and ``InsightsReport`` render the status and insights text."""

from __future__ import annotations

from quarry.reports import StatusReport

_BASE = {
    "document_count": 45,
    "collection_count": 3,
    "chunk_count": 1234,
    "registered_directories": 2,
    "database_path": "/home/user/.punt-labs/quarry/lancedb",
    "embedding_model": "arctic-embed",
}


def test_includes_all_fields() -> None:
    result = StatusReport(_BASE).render()

    assert "quarry status" in result
    assert "1,234" in result
    assert "/home/user/.punt-labs/quarry/lancedb" in result
    assert "arctic-embed" in result


def test_omits_the_retired_tree_walking_size() -> None:
    assert "Size:" not in StatusReport(_BASE).render()


def test_includes_provider() -> None:
    result = StatusReport({**_BASE, "provider": "CPUExecutionProvider (int8)"}).render()

    assert "CPUExecutionProvider (int8)" in result


def test_provider_defaults_to_unknown() -> None:
    assert "Provider:       ?" in StatusReport(_BASE).render()


def test_missing_size_fields_render_as_zero() -> None:
    result = StatusReport(_BASE).render()

    assert "0.00 GiB on disk, 0.00 GiB live" in result
