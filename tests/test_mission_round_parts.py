"""Behaviour of the ``results.yaml`` / ``reflections.yaml`` entry parsers."""

from __future__ import annotations

import pytest

from quarry.mission_round_parts import EvaluatorReflection, RoundEntry, WorkerResult


class TestRoundEntry:
    """The readers both halves share: each degrades to a default, only round raises."""

    def test_text_stringifies_scalars_and_blanks_none(self) -> None:
        assert RoundEntry.text({"k": "v"}, "k") == "v"
        assert RoundEntry.text({"k": 7}, "k") == "7"
        assert RoundEntry.text({"k": None}, "k") == ""
        assert RoundEntry.text({}, "k") == ""

    def test_number_rejects_bool_and_non_numeric(self) -> None:
        assert RoundEntry.number({"c": 0.85}, "c") == 0.85
        assert RoundEntry.number({"c": 1}, "c") == 1.0
        assert RoundEntry.number({"c": True}, "c") == 0.0
        assert RoundEntry.number({"c": "high"}, "c") == 0.0

    def test_strings_coerces_items_and_ignores_non_lists(self) -> None:
        assert RoundEntry.strings({"s": ["a", 2]}, "s") == ("a", "2")
        assert RoundEntry.strings({"s": "a"}, "s") == ()

    def test_is_a_stateless_namespace(self) -> None:
        assert RoundEntry.__slots__ == ()


class TestWorkerResult:
    def test_parses_the_real_shape(self) -> None:
        result = WorkerResult.from_mapping(
            {
                "mission": "m-1",
                "round": 1,
                "author": "gvr",
                "verdict": "pass",
                "confidence": 0.85,
                "open_questions": ["keep the addition?"],
                "prose": "Design complete.\n",
            }
        )
        assert result.round == 1
        assert result.author == "gvr"
        assert result.verdict == "pass"
        assert result.confidence == 0.85
        assert result.open_questions == ("keep the addition?",)
        assert result.prose == "Design complete."

    def test_missing_optional_fields_read_as_empty(self) -> None:
        result = WorkerResult.from_mapping({"round": 2})
        assert result.verdict == ""
        assert result.confidence == 0.0
        assert result.open_questions == ()
        assert result.prose == ""

    @pytest.mark.parametrize("bad", ["1", 1.5, None, True])
    def test_non_integer_round_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValueError, match="round must be an integer"):
            WorkerResult.from_mapping({"round": bad})

    def test_non_list_open_questions_read_as_empty(self) -> None:
        result = WorkerResult.from_mapping({"round": 1, "open_questions": "x"})
        assert result.open_questions == ()


class TestEvaluatorReflection:
    def test_parses_the_real_shape(self) -> None:
        reflection = EvaluatorReflection.from_mapping(
            {
                "round": 1,
                "author": "claude",
                "converging": True,
                "signals": ["evaluator djb: REJECT", "major — aborts"],
                "recommendation": "continue",
                "reason": "  stands  ",
            }
        )
        assert reflection.converging is True
        assert reflection.signals == ("evaluator djb: REJECT", "major — aborts")
        assert reflection.recommendation == "continue"
        assert reflection.reason == "stands"

    def test_converging_is_false_unless_literally_true(self) -> None:
        for value in ("true", 1, None, False):
            assert (
                EvaluatorReflection.from_mapping(
                    {"round": 1, "converging": value}
                ).converging
                is False
            )

    def test_non_integer_round_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="round must be an integer"):
            EvaluatorReflection.from_mapping({"round": "one"})
