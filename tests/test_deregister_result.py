"""Tests for the DeregisterResult JSON shape."""

from __future__ import annotations

from quarry.deregister_result import DeregisterResult


class TestAsDict:
    def test_round_trips_three_fields(self) -> None:
        result = DeregisterResult("docs", 3, 4)
        assert result.as_dict() == {
            "collection": "docs",
            "removed": 3,
            "deleted_chunks": 4,
        }
