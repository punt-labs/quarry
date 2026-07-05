"""Unit tests for Corpus: fixture discovery and the unique-basename guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tools.eval.corpus import Corpus

if TYPE_CHECKING:
    from pathlib import Path


def test_duplicate_basename_across_subdirs_raises(tmp_path: Path) -> None:
    # Two README.md files in different subdirs share a basename. Because
    # document_name == path.name is the qrels/JudgedUnit join key, ingesting
    # both would silently overwrite one in LanceDB. The constructor must reject
    # them at load time, naming both colliding paths.
    code = tmp_path / "code"
    docs = tmp_path / "docs"
    code.mkdir()
    docs.mkdir()
    first = code / "README.md"
    second = docs / "README.md"
    first.write_text("code readme")
    second.write_text("docs readme")

    with pytest.raises(ValueError, match=r"README\.md") as excinfo:
        Corpus(tmp_path)

    message = str(excinfo.value)
    assert "unique" in message
    assert str(first) in message
    assert str(second) in message


def test_unique_basenames_load_cleanly(tmp_path: Path) -> None:
    # Distinct basenames in nested subdirs are the valid case: no collision,
    # every fixture survives into the discovered document set.
    (tmp_path / "code").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "code" / "engine.py").write_text("x = 1")
    (tmp_path / "docs" / "guide.md").write_text("guide")

    corpus = Corpus(tmp_path)

    assert sorted(corpus.document_names()) == ["engine.py", "guide.md"]
