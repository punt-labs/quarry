"""Behaviour of the closed ``memory_type`` vocabulary."""

from __future__ import annotations

import pytest

from quarry.memory_types import DECAYABLE_MEMORY_TYPES, MemoryType


class TestParse:
    @pytest.mark.parametrize("member", list(MemoryType))
    def test_round_trips_every_member(self, member: MemoryType) -> None:
        assert MemoryType.parse(member.value) is member

    @pytest.mark.parametrize("raw", ["Facts", "", "lesson ", "facts", "FACT"])
    def test_rejects_unknown_case_and_whitespace_variants(self, raw: str) -> None:
        """Only the exact lowercase member values parse; near-misses are errors."""
        with pytest.raises(ValueError, match="unknown memory_type"):
            MemoryType.parse(raw)

    def test_error_message_names_the_agent_choices(self) -> None:
        """The message is the 400 body every write route returns."""
        with pytest.raises(ValueError) as excinfo:
            MemoryType.parse("facts")
        assert str(excinfo.value) == (
            "unknown memory_type 'facts'; expected one of "
            "fact, observation, opinion, procedure"
        )


class TestDecay:
    def test_only_lesson_is_not_decayable(self) -> None:
        not_decayable = [m for m in MemoryType if not m.is_decayable]
        assert not_decayable == [MemoryType.LESSON]

    def test_decayable_set_matches_fusion_golden(self) -> None:
        """The set fusion.py hard-coded before the vocabulary moved here.

        Guards that the move changed no ranking: a member added or dropped
        here would silently change which rows decay.
        """
        golden = frozenset({"fact", "observation", "opinion", "procedure"})
        assert golden == DECAYABLE_MEMORY_TYPES

    def test_agent_choices_is_sorted_and_excludes_lesson(self) -> None:
        assert MemoryType.agent_choices() == "fact, observation, opinion, procedure"


class TestWriteRejection:
    def test_empty_is_unset_and_passes(self) -> None:
        assert MemoryType.write_rejection("") == ""

    @pytest.mark.parametrize("raw", ["fact", "observation", "opinion", "procedure"])
    def test_agent_types_pass(self, raw: str) -> None:
        assert MemoryType.write_rejection(raw) == ""

    def test_lesson_is_reserved_for_learn(self) -> None:
        assert (
            MemoryType.write_rejection("lesson")
            == "memory_type 'lesson' is reserved for quarry learn"
        )

    def test_unknown_names_the_choices(self) -> None:
        assert MemoryType.write_rejection("facts") == (
            "unknown memory_type 'facts'; expected one of "
            "fact, observation, opinion, procedure"
        )


class TestStrEnumSemantics:
    def test_member_compares_equal_to_its_string(self) -> None:
        """Row values are plain strings; the enum must compare against them."""
        row_value: str = "lesson"
        assert row_value == MemoryType.LESSON
        assert "fact" in DECAYABLE_MEMORY_TYPES

    def test_formats_as_bare_value(self) -> None:
        assert f"{MemoryType.LESSON}" == "lesson"
