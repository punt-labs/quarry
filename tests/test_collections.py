from __future__ import annotations

from pathlib import Path

import pytest

from quarry.collections import derive_collection, validate_collection_name


class TestDeriveCollection:
    def test_explicit_override(self, tmp_path: Path):
        result = derive_collection(tmp_path / "file.pdf", explicit="my-course")
        assert result == "my-course"

    def test_from_parent_directory(self, tmp_path: Path):
        sub = tmp_path / "ml-101"
        sub.mkdir()
        result = derive_collection(sub / "notes.pdf")
        assert result == "ml-101"

    def test_explicit_takes_precedence(self, tmp_path: Path):
        sub = tmp_path / "some-dir"
        sub.mkdir()
        result = derive_collection(sub / "file.pdf", explicit="override")
        assert result == "override"

    def test_strips_whitespace_from_explicit(self):
        result = derive_collection(Path("/tmp/file.pdf"), explicit="  math  ")
        assert result == "math"

    def test_rejects_empty_explicit(self):
        with pytest.raises(ValueError, match="must not be empty"):
            derive_collection(Path("/tmp/file.pdf"), explicit="")

    def test_rejects_whitespace_only_explicit(self):
        with pytest.raises(ValueError, match="must not be empty"):
            derive_collection(Path("/tmp/file.pdf"), explicit="   ")


class TestValidateCollectionName:
    def test_valid_name(self):
        assert validate_collection_name("ml-101") == "ml-101"

    def test_strips_whitespace(self):
        assert validate_collection_name("  physics  ") == "physics"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_collection_name("")

    def test_rejects_single_quote(self):
        with pytest.raises(ValueError, match="single quotes"):
            validate_collection_name("O'Reilly")

    def test_allows_hyphens_underscores_dots(self):
        assert validate_collection_name("my-course_2024.v1") == "my-course_2024.v1"

    def test_allows_unicode(self):
        assert validate_collection_name("mathematik") == "mathematik"
