"""ReadFault: a transcript that could not be read, told by what it means."""

from __future__ import annotations

import errno

from quarry.read_fault import ReadFault
from quarry.transcript_refusals import NotARegularFileError


def test_a_file_that_vanished_is_not_a_fault_of_the_run() -> None:
    assert ReadFault(FileNotFoundError(errno.ENOENT, "gone")).vanished


def test_any_other_fault_names_its_errno_and_never_quotes_a_path() -> None:
    fault = ReadFault(OSError(errno.EMFILE, "Too many open files", "/home/me/s.jsonl"))

    assert not fault.vanished
    assert fault.reason == "unreadable (EMFILE)"


def test_a_bad_encoding_is_named_by_its_class() -> None:
    fault = ReadFault(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))

    assert fault.reason == "unreadable (UnicodeDecodeError)"


def test_a_refusal_keeps_its_own_words() -> None:
    fault = ReadFault(NotARegularFileError("transcript is not a regular file"))

    assert not fault.vanished
    assert fault.reason == "transcript is not a regular file"
