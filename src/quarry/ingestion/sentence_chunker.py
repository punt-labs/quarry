"""Split text into overlapping chunks at sentence boundaries, never exceeding a size."""

from __future__ import annotations

import re
from typing import ClassVar, Self, final


@final
class SentenceChunker:
    """Greedy sentence packer with overlap, hard-wrapping any overlong sentence.

    Every chunk is at most ``max_chars`` characters: a sentence longer than that
    (a table, a minified file, a PDF stream with no punctuation) is wrapped at
    whitespace, else exactly at ``max_chars``, so stored text equals embedded text.
    Each chunk after the first opens with the tail of the one before it (trimmed
    to a word boundary, and only when there is room for it) for context across
    the seam.
    """

    __slots__ = ("_max_chars", "_overlap_chars")

    _SENTENCE_END: ClassVar[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")
    _SPACES: ClassVar[str] = " \n\t"

    _max_chars: int
    _overlap_chars: int

    def __new__(cls, max_chars: int, overlap_chars: int) -> Self:
        if max_chars < 1:
            msg = f"max_chars must be >= 1, got {max_chars}"
            raise ValueError(msg)
        if overlap_chars < 0:
            msg = f"overlap_chars must be >= 0, got {overlap_chars}"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._max_chars = max_chars
        self._overlap_chars = overlap_chars
        return self

    def split(self, text: str) -> list[str]:
        """Return *text* as chunks of at most ``max_chars`` characters."""
        if len(text) <= self._max_chars:
            return [text]
        chunks: list[str] = []
        parts: list[str] = []  # the overlap tail, then the sentences packed so far
        fresh = 0  # sentences in ``parts`` that are not overlap
        size = 0  # length of " ".join(parts)
        for sentence in self._sentences(text):
            if not self._fits(size, parts, sentence):
                if fresh:
                    chunk = " ".join(parts)
                    chunks.append(chunk)
                    tail = self._tail(chunk)
                    parts, size, fresh = ([tail], len(tail), 0) if tail else ([], 0, 0)
                if parts and not self._fits(size, parts, sentence):
                    parts, size = [], 0  # no room for the overlap: drop it
            size += len(sentence) + (1 if parts else 0)
            parts.append(sentence)
            fresh += 1
        if fresh:
            chunks.append(" ".join(parts))
        return chunks

    def _fits(self, size: int, parts: list[str], sentence: str) -> bool:
        return size + (1 if parts else 0) + len(sentence) <= self._max_chars

    def _sentences(self, text: str) -> list[str]:
        """Return the sentences of *text*, each wrapped to at most ``max_chars``."""
        sentences: list[str] = []
        for sentence in self._SENTENCE_END.split(text):
            if len(sentence) <= self._max_chars:
                if sentence:
                    sentences.append(sentence)
            else:
                sentences.extend(p for p in self._wrap(sentence) if p.strip())
        return sentences

    def _wrap(self, run: str) -> list[str]:
        """Wrap *run* at whitespace within ``max_chars``, else cut exactly there."""
        pieces: list[str] = []
        start = 0
        while len(run) - start > self._max_chars:
            window_end = start + self._max_chars
            gap = max(run.rfind(ch, start + 1, window_end + 1) for ch in self._SPACES)
            if gap == -1:
                pieces.append(run[start:window_end])
                start = window_end
            else:
                pieces.append(run[start:gap])
                start = gap + 1
        if start < len(run):
            pieces.append(run[start:])
        return pieces

    def _tail(self, chunk: str) -> str:
        """Return the overlap carried into the next chunk, starting at a word."""
        if self._overlap_chars == 0 or len(chunk) <= self._overlap_chars:
            return ""
        tail = chunk[-self._overlap_chars :]
        space = tail.find(" ")
        return tail[space + 1 :] if space != -1 else tail
