"""Tests for Utf8Source: UTF-8 text intake that refuses binary content."""

from __future__ import annotations

import io
from typing import NoReturn

import pytest

from quarry.cli_ingest_input import Utf8Source


class _RefusalError(Exception):
    """Raised by the test refusal callback in place of typer.Exit."""


def _fail(message: str) -> NoReturn:
    raise _RefusalError(message)


class TestReadStdin:
    def test_returns_utf8_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = io.TextIOWrapper(io.BytesIO("héllo".encode()), encoding="utf-8")
        monkeypatch.setattr("sys.stdin", stdin)
        assert Utf8Source(_fail).read_stdin() == "héllo"

    def test_refuses_non_utf8_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(b"%PDF-1.7\xff\xfe\x80"), encoding="utf-8")
        monkeypatch.setattr("sys.stdin", stdin)
        with pytest.raises(_RefusalError, match="stdin is not UTF-8 text"):
            Utf8Source(_fail).read_stdin()


class TestRequire:
    def test_accepts_valid_text(self) -> None:
        Utf8Source(_fail).require("a lesson — with unicode", "lesson")

    def test_refuses_surrogate_escaped_bytes(self) -> None:
        bad = b"\xff\xfe".decode("utf-8", errors="surrogateescape")
        with pytest.raises(_RefusalError, match="lesson is not UTF-8 text"):
            Utf8Source(_fail).require(bad, "lesson")
