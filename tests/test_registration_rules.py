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
        with pytest.raises(RegistrationRefusedError, match="covered by parent"):
            RegistrationRules.refuse_if_covered(
                [_reg(str(tmp_path), "parent")], child.resolve()
            )

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

    def test_a_different_directory_is_refused(self, tmp_path: Path) -> None:
        marker = RetainedMarker("docs", "/elsewhere")
        with pytest.raises(RegistrationRefusedError, match="archived"):
            RegistrationRules.refuse_foreign_retained(marker, tmp_path, "docs")

    def test_a_legacy_marker_without_an_origin_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RegistrationRefusedError, match="an unknown directory"):
            RegistrationRules.refuse_foreign_retained(
                RetainedMarker("docs", ""), tmp_path, "docs"
            )


class TestIntegrityRefusal:
    def test_a_registered_directory_names_its_collection(self, tmp_path: Path) -> None:
        refusal = RegistrationRules.integrity_refusal(
            (str(tmp_path), "docs"), tmp_path, "other"
        )
        assert "already registered" in str(refusal)
        assert "'docs'" in str(refusal)

    def test_a_taken_name_is_reported_as_in_use(self, tmp_path: Path) -> None:
        refusal = RegistrationRules.integrity_refusal(
            ("/elsewhere", "docs"), tmp_path, "docs"
        )
        assert str(refusal) == "Collection name already in use: 'docs'"
