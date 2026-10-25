"""TranscriptLoader: one strict read, whose text, stamp and parse cannot disagree."""

from __future__ import annotations

import builtins
import errno
import json
import os
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from quarry.file_stamp import FileStamp
from quarry.transcript_loader import TranscriptLoader
from quarry.transcript_reader import TranscriptReader
from quarry.transcript_refusals import NotARegularFileError, TranscriptTooLargeError

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


def _transcript(tmp_path: Path, text: str = "hello") -> Path:
    path = tmp_path / "session.jsonl"
    record = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_the_conversation_text_and_the_stamp_come_from_the_one_read(
    tmp_path: Path,
) -> None:
    path = _transcript(tmp_path, "hello there")

    loaded = TranscriptLoader.load(path)

    assert "hello there" in loaded.text
    assert loaded.read.transcript == path
    assert loaded.read.stamp == FileStamp.of(path.stat())


def _fails_to_open(code: int) -> AbstractContextManager[object]:
    real_open = builtins.open

    def fails(path: Path, *args: Any, **kwargs: Any) -> object:
        if Path(path).suffix == ".jsonl":
            raise OSError(code, os.strerror(code))
        return real_open(path, *args, **kwargs)

    return patch("quarry.transcript_loader.open", fails, create=True)


def test_the_file_is_opened_once_and_never_read_a_second_time(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    opened: list[str] = []
    real_open = builtins.open

    def counting(target: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(target).name)
        return real_open(target, *args, **kwargs)

    def no_second_read(self: Path, *args: object, **kwargs: object) -> str:
        pytest.fail(f"{self.name} was read a second time")

    with (
        patch("quarry.transcript_loader.open", counting, create=True),
        patch.object(Path, "read_text", no_second_read),
    ):
        TranscriptLoader.load(path)

    assert opened == ["session.jsonl"]


@pytest.mark.parametrize("code", [errno.EMFILE, errno.EACCES, errno.EIO])
def test_a_read_fault_raises_with_its_real_errno(tmp_path: Path, code: int) -> None:
    path = _transcript(tmp_path)

    with _fails_to_open(code), pytest.raises(OSError) as raised:
        TranscriptLoader.load(path)

    assert raised.value.errno == code


def test_a_file_that_is_gone_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as raised:
        TranscriptLoader.load(tmp_path / "gone.jsonl")

    assert raised.value.errno == errno.ENOENT


def test_a_file_that_is_not_utf8_raises_instead_of_reading_as_empty(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"\xff\xfe\x00 not utf-8")

    with pytest.raises(UnicodeDecodeError):
        TranscriptLoader.load(path)


def test_a_transcript_over_the_limit_is_refused_before_it_is_read(
    tmp_path: Path,
) -> None:
    path = _transcript(tmp_path)

    with (
        patch.object(TranscriptLoader, "MAX_BYTES", 10),
        patch.object(TranscriptReader, "from_text") as parsed,
        pytest.raises(TranscriptTooLargeError) as raised,
    ):
        TranscriptLoader.load(path)

    parsed.assert_not_called()
    assert raised.value.read.stamp == FileStamp.of(path.stat())  # kept for the cache


def test_the_refusal_names_the_limit_it_derives_from_the_cap(tmp_path: Path) -> None:
    path = _transcript(tmp_path)

    with (
        patch.object(TranscriptLoader, "MAX_BYTES", 3 * 1024 * 1024),
        patch("quarry.transcript_loader.os.fstat") as fstat,
        pytest.raises(TranscriptTooLargeError, match="3 MiB backfill limit"),
    ):
        fstat.return_value = os.stat_result(
            (stat.S_IFREG | 0o600, 1, 1, 1, 0, 0, 4 << 20, 0, 0, 0)
        )
        TranscriptLoader.load(path)


def test_the_limit_is_32_mib_and_the_cost_of_one_at_the_cap_is_stated() -> None:
    assert TranscriptLoader.MAX_BYTES == 32 * 1024 * 1024
    assert "270 MiB" in (TranscriptLoader.__doc__ or "")
    assert "201 MiB" in (TranscriptLoader.__doc__ or "")


def test_a_file_that_grew_while_it_was_read_is_refused(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    small = path.stat()

    with (
        patch.object(TranscriptLoader, "MAX_BYTES", small.st_size - 1),
        patch("quarry.transcript_loader.os.fstat", return_value=small),
        pytest.raises(TranscriptTooLargeError),
    ):
        TranscriptLoader.load(path)


def test_a_transcript_exactly_at_the_limit_is_read(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    size = path.stat().st_size

    with patch.object(TranscriptLoader, "MAX_BYTES", size):
        assert TranscriptLoader.load(path).text


def test_a_pipe_is_refused_at_once_and_never_blocks_the_scan(tmp_path: Path) -> None:
    fifo = tmp_path / "session.jsonl"
    os.mkfifo(fifo)
    started = time.monotonic()

    with pytest.raises(NotARegularFileError, match="not a regular file"):
        TranscriptLoader.load(fifo)

    assert time.monotonic() - started < 2.0


def test_a_symlink_is_refused_as_not_a_regular_file_and_never_followed(
    tmp_path: Path,
) -> None:
    target = _transcript(tmp_path)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)

    with pytest.raises(NotARegularFileError, match="not a regular file") as raised:
        TranscriptLoader.load(link)

    assert raised.value.read.transcript == link  # the stamp is the link's own


def test_a_refusal_carries_the_read_it_was_made_about(tmp_path: Path) -> None:
    path = _transcript(tmp_path)

    with (
        patch.object(TranscriptLoader, "MAX_BYTES", 1),
        pytest.raises(TranscriptTooLargeError) as raised,
    ):
        TranscriptLoader.load(path)

    assert raised.value.read.transcript == path
    assert str(raised.value) == "transcript is over the 0 MiB backfill limit"


def test_a_directory_is_not_read_and_says_so_by_errno(tmp_path: Path) -> None:
    directory = tmp_path / "session.jsonl"
    directory.mkdir()

    with pytest.raises(OSError) as raised:
        TranscriptLoader.load(directory)

    assert raised.value.errno == errno.EISDIR


def test_the_reader_is_final() -> None:
    assert getattr(TranscriptReader, "__final__", False) is True
