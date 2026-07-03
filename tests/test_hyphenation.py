"""Unit tests for the line-merge / de-hyphenation policy (hyphenation)."""

from __future__ import annotations

from quarry.ingestion.hyphenation import Dehyphenator


class TestMergePlainFragments:
    def test_empty_accumulator_returns_addition(self) -> None:
        assert Dehyphenator.merge("", "first piece") == "first piece"

    def test_non_hyphen_fragments_join_with_space(self) -> None:
        assert Dehyphenator.merge("the quick", "brown fox") == "the quick brown fox"

    def test_numeric_range_joins_without_space(self) -> None:
        assert Dehyphenator.merge("range 10-", "20 units") == "range 10-20 units"

    def test_word_hyphen_before_bracket_keeps_hyphen(self) -> None:
        # Next line starts with "(" -> no letter to merge; keep the hyphen, do
        # not glue a fabricated token ("inter(national)").
        assert Dehyphenator.merge("the inter-", "(national) body") == (
            "the inter-(national) body"
        )

    def test_word_hyphen_before_digit_keeps_hyphen(self) -> None:
        assert Dehyphenator.merge("see page-", "3 for details") == (
            "see page-3 for details"
        )


class TestMergeDehyphenation:
    def test_strips_wrap_hyphen_by_default(self) -> None:
        assert Dehyphenator.merge("the informa-", "tion system.") == (
            "the information system."
        )
        assert Dehyphenator.merge("under develop-", "ment now.") == (
            "under development now."
        )

    def test_strips_clear_fragment(self) -> None:
        assert Dehyphenator.merge("It was inas-", "much a fragment.") == (
            "It was inasmuch a fragment."
        )

    def test_keeps_compound_prefix(self) -> None:
        assert Dehyphenator.merge("a self-", "aware agent.") == "a self-aware agent."
        assert Dehyphenator.merge("It is a well-", "known result.") == (
            "It is a well-known result."
        )

    def test_keeps_prefix_co(self) -> None:
        assert Dehyphenator.merge("They will co-", "operate soon.") == (
            "They will co-operate soon."
        )

    def test_keeps_full_compound(self) -> None:
        assert Dehyphenator.merge("it is read-", "only mode.") == (
            "it is read-only mode."
        )

    def test_preserves_leading_and_trailing_context(self) -> None:
        assert Dehyphenator.merge("see the multi-", "part; done.") == (
            "see the multi-part; done."
        )
