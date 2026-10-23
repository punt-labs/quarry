"""Tests for quarry.registration_rules: the pure registration-refusal decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.collection_marker_store import RetainedMarker
from quarry.refusal import RegistrationRefusedError
from quarry.registration_rules import RegistrationRules
from quarry.sync_registry import DirectoryRegistration


def _reg(directory: str, collection: str) -> DirectoryRegistration:
    return DirectoryRegistration(
        directory=directory, collection=collection, registered_at="2026-01-01"
    )


class TestRefuseIfCovered:
    def test_a_child_of_a_registered_parent_is_refused(self, tmp_path: Path) -> None:
        child = tmp_path / "sub"
        child.mkdir()
        with pytest.raises(RegistrationRefusedError) as info:
            RegistrationRules.refuse_if_covered(
                [_reg(str(tmp_path), "parent")], child.resolve()
            )
        assert str(info.value) == (
            "directory covered by a parent registration; the parent registration "
            "already indexes it; deregister the parent to register this directory "
            "on its own " + _NOTE
        )
        assert info.value.collection == "parent"
        assert info.value.directory == str(tmp_path)

    def test_the_same_directory_is_not_covered_by_itself(self, tmp_path: Path) -> None:
        RegistrationRules.refuse_if_covered(
            [_reg(str(tmp_path), "parent")], tmp_path.resolve()
        )


class TestRefuseForeignRetained:
    def test_no_marker_allows_the_name(self, tmp_path: Path) -> None:
        RegistrationRules.refuse_foreign_retained(None, tmp_path, "docs")

    def test_the_original_directory_may_re_adopt_its_name(self, tmp_path: Path) -> None:
        marker = RetainedMarker("docs", str(tmp_path))
        RegistrationRules.refuse_foreign_retained(marker, tmp_path, "docs")

    def test_a_different_directory_is_refused_with_fixed_text(
        self, tmp_path: Path
    ) -> None:
        marker = RetainedMarker("docs", "/elsewhere")
        with pytest.raises(RegistrationRefusedError) as info:
            RegistrationRules.refuse_foreign_retained(marker, tmp_path, "docs")
        assert str(info.value) == (
            "collection holds archived data from another directory; choose "
            "another collection name, or re-register the directory that owns it "
            + _NOTE
        )
        assert info.value.collection == "docs"
        assert "/elsewhere" in info.value.detail

    def test_a_legacy_marker_without_an_origin_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RegistrationRefusedError) as info:
            RegistrationRules.refuse_foreign_retained(
                RetainedMarker("docs", ""), tmp_path, "docs"
            )
        assert str(info.value) == (
            "collection holds archived data from another directory; choose "
            "another collection name, or re-register the directory that owns it "
            + _NOTE
        )


_NOTE = "(the conflicting registration is named in the quarry logs)"
_HOSTILE = "https://user:hunter2@evil.example/reset/TOKEN?t=abc\n[CRITICAL] FORGED"


class TestIntegrityRefusal:
    def test_a_registered_directory_has_fixed_text(self, tmp_path: Path) -> None:
        refusal = RegistrationRules.integrity_refusal(
            (str(tmp_path), "docs"), tmp_path, "other"
        )
        assert str(refusal) == (
            "directory already registered; run quarry sync, or deregister it "
            "first " + _NOTE
        )
        assert refusal.collection == "other"

    def test_a_taken_name_has_fixed_text(self, tmp_path: Path) -> None:
        refusal = RegistrationRules.integrity_refusal(
            ("/elsewhere", "docs"), tmp_path, "docs"
        )
        assert str(refusal) == (
            "collection name already in use; choose another collection name " + _NOTE
        )

    def test_caller_supplied_text_never_reaches_a_message(self, tmp_path: Path) -> None:
        refusals = [
            RegistrationRules.integrity_refusal(None, tmp_path, _HOSTILE),
            RegistrationRules.integrity_refusal(
                (str(tmp_path), _HOSTILE), tmp_path, _HOSTILE
            ),
        ]
        for refusal in refusals:
            assert "hunter2" not in str(refusal)
            assert "FORGED" not in str(refusal)
        with pytest.raises(RegistrationRefusedError) as info:
            RegistrationRules.refuse_foreign_retained(
                RetainedMarker(_HOSTILE, _HOSTILE), tmp_path, _HOSTILE
            )
        assert "hunter2" not in str(info.value)
        assert len(str(info.value)) < 200
        with pytest.raises(RegistrationRefusedError) as info:
            RegistrationRules.refuse_if_covered(
                [_reg(str(tmp_path), _HOSTILE)], (tmp_path / "x").resolve()
            )
        assert "hunter2" not in str(info.value)


_NAMES = ("hunter2", "TOKEN123", "evil.example", "FORGED")


def test_every_refusal_keeps_its_remediation_and_none_names_a_caller_value(
    tmp_path: Path,
) -> None:
    hostile = "https://user:hunter2@evil.example/TOKEN123\nFORGED"
    refusals = [
        RegistrationRules.integrity_refusal(None, tmp_path, hostile),
        RegistrationRules.integrity_refusal(
            (str(tmp_path), hostile), tmp_path, hostile
        ),
    ]
    for refusal in refusals:
        assert "named in the quarry logs" in str(refusal)
        assert not [n for n in _NAMES if n in str(refusal)]
        assert hostile in refusal.detail or refusal.collection == hostile
