r"""Expand a base profanity word list into its common English inflections.

A real capture leaked "fucking" and "fucked" unredacted because
:mod:`quarry.scrub` matched only the base forms in ``DEFAULT_PROFANITY``.
:class:`ProfanityInflector` expands each base word into its plural,
past-tense, gerund, and agent-noun forms *before* the whole-word regex is
built, so \b...\b boundary matching — which already keeps a safe substring
like "class" or "embassy" from matching (neither has a word boundary before
its embedded "ass") — protects the inflected forms too.
"""

from __future__ import annotations

from typing import Self, final

__all__ = ["ProfanityInflector"]

# Vowels for the plural/doubling heuristics below (English spelling rules).
_VOWELS = frozenset("aeiou")

# Inflected forms that are separate, unrelated real English lexemes, not
# slurs, excluded so an inflection never redacts ordinary text: "dicker"
# ("to negotiate"), "heller" (coin denomination / surname), "jerker"
# (archaic: "soda jerker"), "craps" (the dice game). Contrast "damning"/
# "damned"/"damner" -- damn's own morphological paradigm, kept matched, same
# as this scrubber's no-sense-discrimination base words ("hell" in "what the
# hell" still redacts). Curated against DEFAULT_PROFANITY's 19 words,
# cross-checked in /usr/share/dict/{web2,web2a,propernames} for EVERY
# inflection (not just ``-er`` -- that's how "craps" slipped through). A
# future addition to that tuple needs the same by-hand check.
_EXCLUDED_INFLECTIONS = frozenset(
    {"dicker", "dickers", "heller", "hellers", "jerker", "jerkers", "craps"}
)


@final
class ProfanityInflector:
    """Expand a base word list into its inflected forms for whole-word matching."""

    __slots__ = ()

    def __new__(cls) -> Self:
        """Create a stateless whole-word inflection expander."""
        return super().__new__(cls)

    def expand(self, bases: list[str]) -> frozenset[str]:
        """Return every base word plus its inflections, minus known collisions."""
        forms: set[str] = set()
        for base in bases:
            forms |= self._inflected_forms(base)
        return frozenset(forms - _EXCLUDED_INFLECTIONS)

    @staticmethod
    def _inflected_forms(word: str) -> frozenset[str]:
        """Return *word* plus its common English inflections.

        Covers the plural/3rd-person ``-s``/``-es``, the past-tense ``-ed``
        and gerund ``-ing``, and the comparative/agent-noun ``-er``/``-ers``.
        Consonant doubling ("crap" -> "crapping") and the silent-``e`` drop
        ("douche" -> "douching") follow standard English spelling so the
        generated forms are real, matchable words, not near-misses.
        """
        past, gerund = ProfanityInflector._verb_forms(word)
        agent, agents = ProfanityInflector._agent_forms(word)
        plural = ProfanityInflector._plural(word)
        return frozenset({word, plural, past, gerund, agent, agents})

    @staticmethod
    def _plural(word: str) -> str:
        """Return the standard English plural of *word*."""
        if word.endswith(("s", "x", "z", "ch", "sh")):
            return word + "es"
        if len(word) > 1 and word.endswith("y") and word[-2] not in _VOWELS:
            return word[:-1] + "ies"
        return word + "s"

    @staticmethod
    def _verb_forms(word: str) -> tuple[str, str]:
        """Return (*past*, *gerund*) for *word*, e.g. ``("crapped", "crapping")``.

        A silent trailing ``e`` (not a double ``ee``) takes ``-d`` on the
        FULL word for the past tense but drops the ``e`` before ``-ing``,
        matching "douche" -> "douched"/"douching" (not "douchd"). A short,
        single-final-consonant word instead doubles that consonant, matching
        "crap" -> "crapped"/"crapping" and "shit" -> "shitted"/"shitting".
        The length cap in :meth:`_should_double_final_consonant` keeps a
        multi-syllable word like "stupid" from producing the non-word
        "stupidded" — English doubles the final consonant of a *stressed*
        final syllable, and length is the cheap proxy for "single syllable"
        this profanity list's roots satisfy.
        """
        if word.endswith("e") and not word.endswith("ee"):
            return word + "d", word[:-1] + "ing"
        if ProfanityInflector._should_double_final_consonant(word):
            doubled = word + word[-1]
            return doubled + "ed", doubled + "ing"
        return word + "ed", word + "ing"

    @staticmethod
    def _agent_forms(word: str) -> tuple[str, str]:
        """Return (*agent*, *agent plural*) for *word*, e.g. ``("fucker", "fuckers")``.

        Shares :meth:`_verb_forms`' e-drop and doubling rules — the agent
        noun is built the same way as the gerund, with ``-er``/``-ers`` in
        place of ``-ing``.
        """
        if word.endswith("e") and not word.endswith("ee"):
            agent = word[:-1] + "er"
        elif ProfanityInflector._should_double_final_consonant(word):
            agent = word + word[-1] + "er"
        else:
            agent = word + "er"
        return agent, agent + "s"

    @staticmethod
    def _should_double_final_consonant(word: str) -> bool:
        """Return whether *word* is short and CVC-shaped enough to double its ending.

        Requires a consonant-vowel-consonant tail (e.g. "c-r-a-p") and a
        final letter outside ``w``/``x``/``y`` (English never doubles those).
        Doubling applies to a *stressed* final syllable -- every monosyllable
        qualifies, but "MOR-on" is stressed on its FIRST syllable, so
        doubling would misspell "moronned". The 3-4 band is this list's
        monosyllabic-root length; "moron" (5, CVC-shaped) is disyllabic and
        excluded rather than mis-doubled.
        """
        if not 3 <= len(word) <= 4:
            return False
        if word[-1] in "wxy":
            return False
        tail_is_cvc = word[-3] not in _VOWELS and word[-2] in _VOWELS
        return tail_is_cvc and word[-1] not in _VOWELS
