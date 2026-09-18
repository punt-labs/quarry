"""Rendering and bucket semantics of :class:`ExtScanOutcome`."""

from __future__ import annotations

from quarry.ethos_ext_scan import ExtFailure, ExtScanOutcome, ExtWriteResult


class TestExtScanOutcome:
    def test_message_golden(self) -> None:
        outcome = ExtScanOutcome(
            updated=("claude",),
            already_set=("rmh", "kpz"),
            no_collection=("ghost",),
            failed=(ExtFailure("bad", "boom"),),
        )
        assert outcome.message() == (
            "updated 1 identity: claude; already set: rmh, kpz; "
            "no memory_collection (check config): ghost; errors: bad: boom"
        )

    def test_already_set_alone_uses_the_long_prefix(self) -> None:
        assert ExtScanOutcome(already_set=("rmh",)).message() == (
            "session_context already set: rmh"
        )

    def test_plural_identities(self) -> None:
        assert ExtScanOutcome(updated=("a", "b")).message() == (
            "updated 2 identities: a, b"
        )

    def test_empty_outcome(self) -> None:
        outcome = ExtScanOutcome()
        assert outcome.is_empty
        assert outcome.message() == ""
        assert outcome.failed_handles == ()

    def test_failed_handles_drop_the_reasons(self) -> None:
        outcome = ExtScanOutcome(
            failed=(ExtFailure("a", "x"), ExtFailure("b", "y")),
        )
        assert outcome.failed_handles == ("a", "b")
        assert not outcome.is_empty


class TestExtWriteResult:
    def test_values_are_the_wire_strings(self) -> None:
        assert [r.value for r in ExtWriteResult] == [
            "updated",
            "already_set",
            "no_collection",
        ]
