"""Tests for quarry.ingestion.sentence_chunker: bounded, overlapping chunks."""

from __future__ import annotations

from itertools import pairwise

import pytest

from quarry.ingestion.sentence_chunker import SentenceChunker


class TestSplit:
    def test_short_text_no_split(self) -> None:
        assert SentenceChunker(100, 20).split("Hello world.") == ["Hello world."]

    def test_exact_boundary(self) -> None:
        text = "a" * 100
        assert SentenceChunker(100, 20).split(text) == [text]

    def test_splits_at_sentence_boundary(self) -> None:
        text = "First sentence. Second sentence. Third sentence."
        result = SentenceChunker(35, 10).split(text)
        assert len(result) >= 2
        assert "First sentence." in result[0]

    def test_chunks_open_with_the_tail_of_the_previous_one(self) -> None:
        text = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
        result = SentenceChunker(30, 10).split(text)
        assert len(result) >= 2
        for previous, current in pairwise(result):
            seam = current.split(" ")[0]
            assert seam in previous

    def test_no_overlap_when_it_is_zero(self) -> None:
        result = SentenceChunker(20, 0).split("Alpha beta. Gamma delta. Epsilon zeta.")
        assert result == ["Alpha beta.", "Gamma delta.", "Epsilon zeta."]

    def test_every_chunk_is_at_most_max_chars(self) -> None:
        text = " ".join(f"Sentence number {n} ends here." for n in range(200))
        chunks = SentenceChunker(120, 30).split(text)
        assert max(len(c) for c in chunks) <= 120

    def test_all_the_text_is_kept(self) -> None:
        sentences = [f"Sentence number {n} ends here." for n in range(100)]
        chunks = SentenceChunker(120, 0).split(" ".join(sentences))
        assert " ".join(chunks) == " ".join(sentences)


class TestHardWrap:
    def test_a_long_run_is_wrapped_at_max_chars(self) -> None:
        result = SentenceChunker(100, 0).split("a" * 250)
        assert [len(c) for c in result] == [100, 100, 50]

    def test_a_wrapped_run_overlaps_within_the_bound(self) -> None:
        result = SentenceChunker(100, 20).split("a" * 250)
        assert [len(c) for c in result] == [100, 100, 71]

    def test_a_long_run_prefers_whitespace(self) -> None:
        text = "alpha beta gamma delta " * 20
        for chunk in SentenceChunker(50, 0).split(text):
            assert len(chunk) <= 50
            assert not chunk.startswith(("lpha", "eta", "amma", "elta"))

    def test_an_unpunctuated_422k_run_wraps(self) -> None:
        result = SentenceChunker(1800, 200).split("x" * 422_780)
        assert max(len(c) for c in result) <= 1800
        assert len(result) >= 422_780 // 1800

    def test_an_overlong_sentence_between_short_ones_keeps_the_bound(self) -> None:
        text = "Short one. " + "y" * 400 + ". Short two."
        result = SentenceChunker(100, 30).split(text)
        assert max(len(c) for c in result) <= 100
        assert result[0].startswith("Short one.")
        assert result[-1].endswith("Short two.")

    def test_an_overlap_tail_that_leaves_no_room_is_dropped(self) -> None:
        # Each sentence fills the chunk, so no overlap can precede the next one.
        text = ("a" * 50 + ". ") * 4
        result = SentenceChunker(51, 40).split(text.strip())
        assert all(len(c) <= 51 for c in result)
        assert all(c.count("a") <= 50 for c in result)


class TestConstruction:
    def test_a_non_positive_size_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_chars"):
            SentenceChunker(0, 0)

    def test_a_negative_overlap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="overlap_chars"):
            SentenceChunker(20, -5)
