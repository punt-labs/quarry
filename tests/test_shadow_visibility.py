"""Tests for Visibility: gh string and JSON payload parsing (bug class 4)."""

from __future__ import annotations

import pytest

from quarry.shadow.visibility import Visibility


class TestFromGh:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("public", Visibility.PUBLIC),
            ("PUBLIC", Visibility.PUBLIC),
            ("  Private  ", Visibility.PRIVATE),
            ("private", Visibility.PRIVATE),
            ("internal", Visibility.UNKNOWN),
            ("", Visibility.UNKNOWN),
        ],
    )
    def test_maps_string(self, value: str, expected: Visibility) -> None:
        assert Visibility.from_gh(value) is expected


class TestFromJson:
    def test_valid_payload(self) -> None:
        assert Visibility.from_json('{"visibility": "PRIVATE"}') is Visibility.PRIVATE

    def test_missing_key_is_unknown(self) -> None:
        assert Visibility.from_json("{}") is Visibility.UNKNOWN

    def test_malformed_json_is_unknown_not_raise(self) -> None:
        # A payload gh/git could not produce as valid JSON is unverifiable, not
        # public/private: fall back to UNKNOWN so the gate refuses or acks.
        assert Visibility.from_json("not json") is Visibility.UNKNOWN
