"""Tests for quarry.capture — the CaptureWriter choke point.

Covers the recurring bug classes for capture writes: file-I/O safety
(all-or-nothing on failure), and producer divergence (the two producers
must yield byte-identical files for identical input).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from quarry.artifacts import SessionArtifacts
from quarry.capture import CaptureRequest, CaptureWriter

if TYPE_CHECKING:
    import pytest

_EMPTY_ARTIFACTS = SessionArtifacts(
    commit_shas=(),
    pr_numbers=(),
    branch_names=(),
    bead_ids=(),
)


def _request(
    project_dir: Path,
    *,
    label: str = "pre-compact",
    session_id: str = "abcd1234ef",
    text: str = "body text",
) -> CaptureRequest:
    return CaptureRequest(
        project_dir=project_dir,
        session_id=session_id,
        timestamp="2026-07-11T00:00:00Z",
        artifacts=_EMPTY_ARTIFACTS,
        text=text,
        label=label,
    )


# ---------------------------------------------------------------------------
# Happy path + the security property
# ---------------------------------------------------------------------------


def test_write_creates_scrubbed_capture(tmp_path: Path) -> None:
    req = _request(tmp_path, text="path /Users/jane/x mail jdoe@example.com")
    CaptureWriter().write(req)

    path = req.capture_path()
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "/Users/" not in content
    assert "@" not in content
    assert "~/x" in content


def test_empty_frontmatter_writes_no_file(tmp_path: Path) -> None:
    """A blank session_id yields empty frontmatter — nothing is written."""
    req = _request(tmp_path, session_id="")
    CaptureWriter().write(req)
    assert not req.capture_path().exists()


# ---------------------------------------------------------------------------
# Bug class 1 — file-I/O safety / all-or-nothing
# ---------------------------------------------------------------------------


def test_scrub_failure_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If scrubbing raises, no bytes reach disk (scrub precedes write)."""

    def boom(_text: str, _label: str) -> str:
        msg = "scrub exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr("quarry.capture.scrub_and_log", boom)
    req = _request(tmp_path, text="/Users/jane/secret")
    CaptureWriter().write(req)  # must not raise

    assert not req.capture_path().exists()


def test_write_failure_swallowed_leaves_no_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write_text failure is swallowed and leaves neither file nor temp."""
    real_write_text = Path.write_text

    def failing_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.endswith(".tmp"):
            msg = "disk full"
            raise OSError(msg)
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    req = _request(tmp_path)
    CaptureWriter().write(req)  # must not raise

    path = req.capture_path()
    assert not path.exists()
    assert not list(path.parent.glob("*.tmp"))


def test_concurrent_writers_of_one_session_never_clobber_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook and a backfill can write one session at once: each gets a temp file.

    Both writers are held after writing their temp file until the other has
    written its own, so a shared temp name would be written by both before either
    renames it.
    """
    real_write_text = Path.write_text
    both_written = threading.Barrier(2, timeout=10)
    temps: list[str] = []

    def synchronized_write_text(
        self: Path, data: str, encoding: str | None = None
    ) -> int:
        written = real_write_text(self, data, encoding=encoding)
        if self.name.endswith(".tmp"):
            temps.append(self.name)
            both_written.wait()
        return written

    monkeypatch.setattr(Path, "write_text", synchronized_write_text)
    first = _request(tmp_path, text="first writer " * 40)
    second = _request(tmp_path, text="second writer " * 40)
    complete = {first.scrubbed_content(), second.scrubbed_content()}

    with ThreadPoolExecutor(max_workers=2) as pool:
        landed = list(pool.map(CaptureWriter().write, [first, second]))

    assert landed == [True, True]
    assert len(set(temps)) == 2
    assert first.capture_path().read_text() in complete
    assert not list(first.capture_path().parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Bug class 3 — producer divergence
# ---------------------------------------------------------------------------


def test_producers_yield_byte_identical_files(tmp_path: Path) -> None:
    """Same input under each producer label produces byte-identical files."""
    text = "body /Users/jane/x mail jdoe@example.com"
    pre = _request(tmp_path / "pre", label="pre-compact", text=text)
    back = _request(tmp_path / "back", label="backfill", text=text)

    CaptureWriter().write(pre)
    CaptureWriter().write(back)

    assert pre.capture_path().read_bytes() == back.capture_path().read_bytes()


def test_write_reports_whether_a_file_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _request(tmp_path, text="hello world")
    assert CaptureWriter().write(good) is True
    assert CaptureWriter().write(_request(tmp_path, session_id="")) is False

    def boom(_text: str, _label: str) -> str:
        msg = "scrub exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr("quarry.capture.scrub_and_log", boom)
    assert CaptureWriter().write(_request(tmp_path, text="x")) is False
