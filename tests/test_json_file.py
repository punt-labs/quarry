"""JsonFile: a config we do not own is read once, bounded, and refused if wrong."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from unittest.mock import patch

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


def _recording_open(opened: list[int]) -> object:
    real_open = os.open

    def recording(path: Path, flags: int, *args: int) -> int:
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd

    return recording


def _is_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


@pytest.mark.parametrize("failing", ["fstat", "fdopen"])
def test_an_operation_that_raises_still_closes_the_descriptor(
    tmp_path: Path, failing: str
) -> None:
    path = tmp_path / "cfg.json"
    path.write_text("{}")
    opened: list[int] = []

    with (
        patch("quarry.json_file.os.open", _recording_open(opened)),
        patch(f"quarry.json_file.os.{failing}", side_effect=OSError(errno.EIO, "x")),
        pytest.raises(ValueError, match=r"cfg\.json: OSError: EIO"),
    ):
        JsonFile.load(path)

    assert len(opened) == 1
    assert _is_closed(opened[0])


class _BrokenHandle:
    """A file handle whose read fails, as a disk error would."""

    def __enter__(self) -> _BrokenHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        raise OSError(errno.EIO, "read failed")


def test_a_read_that_raises_still_closes_the_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text("{}")
    opened: list[int] = []

    with (
        patch("quarry.json_file.os.open", _recording_open(opened)),
        patch("quarry.json_file.os.fdopen", return_value=_BrokenHandle()),
        pytest.raises(ValueError, match=r"cfg\.json: OSError: EIO"),
    ):
        JsonFile.load(path)

    assert _is_closed(opened[0])


def test_the_refusals_close_the_descriptor_too(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    big = tmp_path / "big.json"
    big.write_text("x" * (JsonFile.MAX_BYTES + 1))
    opened: list[int] = []

    with patch("quarry.json_file.os.open", _recording_open(opened)):
        for path in (fifo, big):
            with pytest.raises(ValueError, match=path.name.replace(".", r"\.")):
                JsonFile.load(path)

    assert len(opened) == 2
    assert all(_is_closed(fd) for fd in opened)
