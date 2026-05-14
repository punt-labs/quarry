from __future__ import annotations

from pathlib import Path

import pytest

from quarry.collections import CollectionName


class TestCollectionNameConstruction:
    def test_valid_name(self) -> None:
        cn = CollectionName("ml-101")
        assert cn.name == "ml-101"

    def test_strips_whitespace(self) -> None:
        cn = CollectionName("  physics  ")
        assert cn.name == "physics"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CollectionName("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CollectionName("   ")

    def test_rejects_single_quote(self) -> None:
        with pytest.raises(ValueError, match="single quotes"):
            CollectionName("O'Reilly")

    def test_allows_hyphens_underscores_dots(self) -> None:
        cn = CollectionName("my-course_2024.v1")
        assert cn.name == "my-course_2024.v1"

    def test_allows_unicode(self) -> None:
        cn = CollectionName("mathematik")
        assert cn.name == "mathematik"

    def test_str_returns_name(self) -> None:
        cn = CollectionName("test-col")
        assert str(cn) == "test-col"

    def test_repr(self) -> None:
        cn = CollectionName("test-col")
        assert repr(cn) == "CollectionName('test-col')"


class TestCollectionNameFlyweight:
    def test_identity_same_name(self) -> None:
        a = CollectionName("x")
        b = CollectionName("x")
        assert a is b

    def test_identity_different_names(self) -> None:
        a = CollectionName("alpha")
        b = CollectionName("beta")
        assert a is not b

    def test_equality(self) -> None:
        a = CollectionName("same")
        b = CollectionName("same")
        assert a == b

    def test_hash_consistency(self) -> None:
        a = CollectionName("hashed")
        b = CollectionName("hashed")
        assert hash(a) == hash(b)

    def test_usable_as_dict_key(self) -> None:
        cn = CollectionName("key")
        d = {cn: 42}
        assert d[CollectionName("key")] == 42

    def test_not_equal_to_string(self) -> None:
        cn = CollectionName("test")
        assert cn != "test"

    def test_whitespace_variants_resolve_to_same(self) -> None:
        """'  x  ' and 'x' resolve to the same cached instance after strip."""
        a = CollectionName("x")
        b = CollectionName("  x  ")
        assert a is b


class TestCollectionNameFromPath:
    def test_explicit_override(self, tmp_path: Path) -> None:
        cn = CollectionName.from_path(tmp_path / "file.pdf", explicit="my-course")
        assert cn.name == "my-course"

    def test_from_parent_directory(self, tmp_path: Path) -> None:
        sub = tmp_path / "ml-101"
        sub.mkdir()
        cn = CollectionName.from_path(sub / "notes.pdf")
        assert cn.name == "ml-101"

    def test_explicit_takes_precedence(self, tmp_path: Path) -> None:
        sub = tmp_path / "some-dir"
        sub.mkdir()
        cn = CollectionName.from_path(sub / "file.pdf", explicit="override")
        assert cn.name == "override"

    def test_strips_whitespace_from_explicit(self) -> None:
        cn = CollectionName.from_path(Path("/tmp/file.pdf"), explicit="  math  ")
        assert cn.name == "math"

    def test_rejects_empty_explicit(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CollectionName.from_path(Path("/tmp/file.pdf"), explicit="")

    def test_rejects_whitespace_only_explicit(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CollectionName.from_path(Path("/tmp/file.pdf"), explicit="   ")
