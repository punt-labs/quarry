"""TranscriptLoader: one strict read, whose text, stamp and parse cannot disagree."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from quarry.file_stamp import FileStamp
from quarry.transcript_loader import TranscriptLoader, TranscriptTooLargeError
from quarry.transcript_reader import TranscriptReader


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


def test_the_file_is_opened_once_and_never_read_a_second_time(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    opened: list[str] = []
    real_open = Path.open

    def counting(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    def no_second_read(self: Path, *args: object, **kwargs: object) -> str:
        pytest.fail(f"{self.name} was read a second time")

    with (
        patch.object(Path, "open", counting),
        patch.object(Path, "read_text", no_second_read),
    ):
        TranscriptLoader.load(path)

    assert opened == ["session.jsonl"]


@pytest.mark.parametrize(
    "code",
    [errno.EMFILE, errno.EACCES, errno.EIO],
)
def test_a_read_fault_raises_with_its_real_errno(tmp_path: Path, code: int) -> None:
    path = _transcript(tmp_path)

    def fails(self: Path, *args: object, **kwargs: object) -> object:
        raise OSError(code, os.strerror(code))

    with patch.object(Path, "open", fails), pytest.raises(OSError) as raised:
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


def test_a_transcript_over_the_limit_is_refused_not_truncated(tmp_path: Path) -> None:
    path = _transcript(tmp_path)

    with (
        patch.object(TranscriptLoader, "MAX_BYTES", 10),
        pytest.raises(TranscriptTooLargeError, match="64 MiB backfill limit"),
    ):
        TranscriptLoader.load(path)


def test_the_limit_is_64_mib_as_the_refusal_says() -> None:
    assert TranscriptLoader.MAX_BYTES == 64 * 1024 * 1024


def test_a_transcript_exactly_at_the_limit_is_read(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    size = path.stat().st_size

    with patch.object(TranscriptLoader, "MAX_BYTES", size):
        assert TranscriptLoader.load(path).text


def test_the_reader_is_final() -> None:
    assert getattr(TranscriptReader, "__final__", False) is True
