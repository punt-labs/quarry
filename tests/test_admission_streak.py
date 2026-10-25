"""AdmissionStreak: a queue that stays full is given up on after a few in a row."""

from __future__ import annotations

from quarry.admission_streak import AdmissionStreak


def test_it_says_give_up_only_after_the_maximum_in_a_row() -> None:
    streak = AdmissionStreak()

    answers = [streak.refused() for _ in range(AdmissionStreak.MAX)]

    assert answers == [False] * (AdmissionStreak.MAX - 1) + [True]


def test_an_answer_from_the_queue_ends_the_streak() -> None:
    streak = AdmissionStreak()
    for _ in range(AdmissionStreak.MAX - 1):
        streak.refused()

    streak.answered()

    assert not streak.refused()
