"""Unit tests for QuerySet loading and the unique-query-id guard."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tools.eval.queryset import QuerySet

if TYPE_CHECKING:
    from pathlib import Path


def _line(query_id: str, text: str) -> str:
    return json.dumps(
        {"id": query_id, "text": text, "bucket": "natural", "split": "dev"}
    )


def test_from_path_rejects_duplicate_query_ids(tmp_path: Path) -> None:
    # Two queries share id "q1". Query ids key the qrels dict and per-query
    # scores, so the second would silently overwrite the first. from_path must
    # fail loud and name the duplicate id.
    path = tmp_path / "queries.jsonl"
    path.write_text(
        _line("q1", "first") + "\n" + _line("q1", "second") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate query id 'q1'"):
        QuerySet.from_path(path)


def test_from_path_loads_unique_query_ids(tmp_path: Path) -> None:
    # Distinct ids load cleanly, preserving file order.
    path = tmp_path / "queries.jsonl"
    path.write_text(
        _line("q1", "first") + "\n" + _line("q2", "second") + "\n",
        encoding="utf-8",
    )
    queryset = QuerySet.from_path(path)
    assert [q.query_id for q in queryset] == ["q1", "q2"]
