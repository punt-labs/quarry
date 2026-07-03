"""De-hyphenation policy for PDF line-break hyphens.

A hyphen at a line break is predominantly wrap hyphenation, so the default is to
strip it and merge the fragments — ``informa-`` + ``tion`` becomes
``information``, a token BM25 and vector search can match. The hyphen is kept
only for genuine compound prefixes (``self-``, ``well-``, ``co-``, …) or known
full compounds, where a merged form would be wrong. When in doubt the bias still
favours a searchable token over a preserved hyphen.
"""

from __future__ import annotations

from dataclasses import dataclass

# Compound prefixes whose hyphen is semantic — keep it across a line break.
_KEEP_HYPHEN_PREFIXES: frozenset[str] = frozenset(
    {"self", "well", "co", "non", "anti", "pre", "re", "multi", "quasi"}
)
# Full hyphenated compounds to preserve even when the prefix is not listed.
_KEEP_HYPHEN_COMPOUNDS: frozenset[str] = frozenset(
    {"state-of-the-art", "read-only", "long-term", "short-term"}
)


@dataclass(frozen=True, slots=True)
class Dehyphenator:
    """Strip-by-default policy for concatenating two wrapped line fragments."""

    @staticmethod
    def merge(accumulated: str, addition: str) -> str:
        """Concatenate a wrapped line's running text with the next fragment.

        A trailing word hyphen is de-hyphenated; a trailing non-word hyphen (a
        numeric range like ``10-`` + ``20``) joins without a space; otherwise the
        two fragments are separated by a single space.
        """
        if not accumulated:
            return addition
        if accumulated.endswith("-") and len(accumulated) >= 2:
            if accumulated[-2].isalpha():
                return Dehyphenator._dehyphenate(accumulated, addition)
            return f"{accumulated}{addition}"
        return f"{accumulated} {addition}"

    @staticmethod
    def _dehyphenate(accumulated: str, addition: str) -> str:
        """Merge a word-hyphen tail with ``addition``, keeping the hyphen only
        for compound prefixes or known compounds.
        """
        before, left = Dehyphenator._split_left(accumulated[:-1])
        right, after = Dehyphenator._split_right(addition)
        keep = Dehyphenator._keep(left, right)
        joined = f"{left}-{right}" if keep else f"{left}{right}"
        return f"{before}{joined}{after}"

    @staticmethod
    def _split_left(prefix: str) -> tuple[str, str]:
        """Split off the trailing alphabetic run: (before, left_fragment)."""
        split = len(prefix)
        while split > 0 and prefix[split - 1].isalpha():
            split -= 1
        return prefix[:split], prefix[split:]

    @staticmethod
    def _split_right(addition: str) -> tuple[str, str]:
        """Split off the leading alphabetic run: (right_fragment, after)."""
        cut = 0
        while cut < len(addition) and addition[cut].isalpha():
            cut += 1
        return addition[:cut], addition[cut:]

    @staticmethod
    def _keep(left: str, right: str) -> bool:
        """Return whether the hyphen between the two fragments is preserved."""
        return (
            left.lower() in _KEEP_HYPHEN_PREFIXES
            or f"{left}-{right}".lower() in _KEEP_HYPHEN_COMPOUNDS
        )
