"""Tests for DeregisterResult wire-boundary coercion and JSON shape."""

from __future__ import annotations

from quarry.deregister_result import DeregisterResult


class TestFromTask:
    def test_happy_path(self):
        result = DeregisterResult.from_task("docs", {"removed": 3, "deleted_chunks": 4})
        assert result.collection == "docs"
        assert result.removed == 3
        assert result.deleted_chunks == 4

    def test_missing_keys_default_to_zero(self):
        # 0 is the documented contract: a completed purge task's informational
        # counts are absent when there is nothing to report, not an error.
        result = DeregisterResult.from_task("docs", {})
        assert result.removed == 0
        assert result.deleted_chunks == 0

    def test_non_numeric_and_none_coerce_to_zero(self):
        result = DeregisterResult.from_task(
            "docs", {"removed": "x", "deleted_chunks": None}
        )
        assert result.removed == 0
        assert result.deleted_chunks == 0

    def test_bool_excluded_from_int_coercion(self):
        # True is an int subclass; it must NOT coerce to 1.
        result = DeregisterResult.from_task(
            "docs", {"removed": True, "deleted_chunks": False}
        )
        assert result.removed == 0
        assert result.deleted_chunks == 0


class TestAsDict:
    def test_round_trips_three_fields(self):
        result = DeregisterResult("docs", 3, 4)
        assert result.as_dict() == {
            "collection": "docs",
            "removed": 3,
            "deleted_chunks": 4,
        }
