"""Tests for LessonComposer (naming) and LessonsCollection (routing)."""

from __future__ import annotations

from quarry.lesson import LessonComposer, LessonsCollection


class TestLessonComposerDocumentName:
    def test_same_name_twice_never_collides(self) -> None:
        """Two lessons filed under the same --name must get distinct keys.

        Guards the exact RRF dedup collision (document_name, chunk_index,
        page_number) the design identified: without a unique suffix, two
        distinct lessons sharing a name would merge scores and drop one.
        """
        first = LessonComposer.document_name("auth-gotcha", "")
        second = LessonComposer.document_name("auth-gotcha", "")
        assert first != second

    def test_name_wins_over_topic(self) -> None:
        name = LessonComposer.document_name("auth-gotcha", "testing")
        assert name.startswith("lesson-auth-gotcha-")

    def test_empty_name_falls_back_to_topic(self) -> None:
        name = LessonComposer.document_name("", "testing")
        assert name.startswith("lesson-testing-")

    def test_empty_both_falls_back_to_note(self) -> None:
        name = LessonComposer.document_name("", "")
        assert name.startswith("lesson-note-")

    def test_slug_lowercases_and_strips_non_alphanumerics(self) -> None:
        name = LessonComposer.document_name("Auth Gotcha!! 2026", "")
        assert name.startswith("lesson-auth-gotcha-2026-")

    def test_slug_truncates_at_forty_chars(self) -> None:
        long_name = "a" * 100
        name = LessonComposer.document_name(long_name, "")
        # "lesson-" + 40 a's + "-" + 8 hex chars
        slug = name.removeprefix("lesson-").rsplit("-", 1)[0]
        assert slug == "a" * 40

    def test_suffix_is_eight_hex_chars(self) -> None:
        name = LessonComposer.document_name("x", "")
        suffix = name.rsplit("-", 1)[1]
        assert len(suffix) == 8
        int(suffix, 16)  # raises ValueError if not hex


class TestLessonsCollection:
    def test_for_repo(self) -> None:
        assert LessonsCollection.for_repo("quarry").name == "quarry-lessons"

    def test_resolve_none_is_default(self) -> None:
        assert LessonsCollection.resolve(None).name == "default-lessons"

    def test_for_cwd_registered_ancestor(self) -> None:
        regs = {"/projects/myapp": "myapp"}
        got = LessonsCollection.for_cwd("/projects/myapp/src/lib", regs)
        assert got.name == "myapp-lessons"

    def test_for_cwd_unregistered_is_default(self) -> None:
        got = LessonsCollection.for_cwd("/somewhere/else", {"/x": "x"})
        assert got.name == "default-lessons"

    def test_for_cwd_relative_path_is_default(self) -> None:
        """A relative cwd never resolves against the daemon's own process cwd."""
        got = LessonsCollection.for_cwd("src", {"/projects/myapp": "myapp"})
        assert got.name == "default-lessons"
