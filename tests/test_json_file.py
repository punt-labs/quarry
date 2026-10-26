"""JsonFile: a config we do not own is read once, bounded, and refused if wrong."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from quarry.json_file import JsonFile


def test_deep_nesting_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000)

    with pytest.raises(ValueError, match=r"deep\.json: not JSON"):
        JsonFile.load(path)


def test_an_oversize_file_is_refused_unread(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_text('{"k": "' + "x" * JsonFile.MAX_BYTES + '"}')

    with pytest.raises(ValueError, match=r"big\.json: larger than"):
        JsonFile.load(path)


def test_an_empty_regular_file_is_an_empty_object(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_bytes(b"")

    assert JsonFile.load(path) == {}


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "fifo.json"
    os.mkfifo(path)

    with pytest.raises(ValueError, match=r"fifo\.json: not a regular file"):
        JsonFile.load(path)
    assert stat.S_ISFIFO(path.stat().st_mode)


@pytest.mark.parametrize(
    ("name", "data"),
    [("latin.json", b"\xff"), ("text.json", b"{nope"), ("list.json", b"[]")],
)
def test_every_message_names_the_file(tmp_path: Path, name: str, data: bytes) -> None:
    (tmp_path / name).write_bytes(data)

    with pytest.raises(ValueError, match=name.replace(".", r"\.")):
        JsonFile.load(tmp_path / name)


def test_object_at_names_the_file_when_the_value_is_not_an_object(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cfg.json"

    with pytest.raises(ValueError, match=r"cfg\.json: mcpServers is not an object"):
        JsonFile.object_at({"mcpServers": "x"}, "mcpServers", source)
