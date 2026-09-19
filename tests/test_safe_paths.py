"""Tests for the symlink-safe repo-relative path writer."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.safe_paths import SafeRepoPath

_RELATIVE = (".punt-labs", "quarry", "leaf")


def _target(root: Path) -> SafeRepoPath:
    return SafeRepoPath(root, _RELATIVE)


# ── normal (real-directory) behaviour ────────────────────────────────


def test_create_exclusive_creates_with_content_and_mode(tmp_path: Path) -> None:
    created = _target(tmp_path).create_exclusive("hello\n", mode=0o644)
    leaf = tmp_path.joinpath(*_RELATIVE)
    assert created is True
    assert leaf.read_text() == "hello\n"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o644


def test_create_exclusive_forces_mode_under_restrictive_umask(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        _target(tmp_path).create_exclusive("x", mode=0o644)
    finally:
        os.umask(old)
    assert stat.S_IMODE(tmp_path.joinpath(*_RELATIVE).stat().st_mode) == 0o644


def test_create_exclusive_existing_regular_is_noop(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert target.create_exclusive("first\n", mode=0o644) is True
    assert target.create_exclusive("second\n", mode=0o644) is False
    assert tmp_path.joinpath(*_RELATIVE).read_text() == "first\n"


def test_is_regular_file_true_then_false_after_remove(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert target.is_regular_file() is False
    target.create_exclusive("x", mode=0o644)
    assert target.is_regular_file() is True
    assert target.remove() is True
    assert target.is_regular_file() is False


def test_write_atomic_overwrites_existing(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.create_exclusive("old\n", mode=0o644)
    target.write_atomic("new\n", mode=0o644)
    leaf = tmp_path.joinpath(*_RELATIVE)
    assert leaf.read_text() == "new\n"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o644
    # No temp file is left behind in the parent directory.
    assert [p.name for p in leaf.parent.iterdir()] == ["leaf"]


def test_remove_absent_is_false(tmp_path: Path) -> None:
    assert _target(tmp_path).remove() is False


# ── write_atomic: mode policy ────────────────────────────────────────


def test_write_atomic_preserves_the_leaf_mode_when_none_is_forced(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    target.create_exclusive("old\n", mode=0o600)
    target.write_atomic("new\n")
    leaf = tmp_path.joinpath(*_RELATIVE)
    assert leaf.read_text() == "new\n"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600


def test_write_atomic_new_leaf_defaults_to_0644_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    old = os.umask(0o077)
    try:
        _target(tmp_path).write_atomic("x\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(tmp_path.joinpath(*_RELATIVE).stat().st_mode) == 0o644


# ── non-regular leaf is refused / ignored ────────────────────────────


def test_create_exclusive_refuses_symlinked_leaf(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("secret\n")
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.symlink_to(outside)

    with pytest.raises(ValueError, match="not a regular file"):
        target.create_exclusive("x", mode=0o644)

    assert outside.read_text() == "secret\n"


def test_create_exclusive_refuses_directory_leaf(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        target.create_exclusive("x", mode=0o644)


def test_write_atomic_refuses_symlinked_leaf(tmp_path: Path) -> None:
    """A committed link is never replaced by our file: refused, link left as found."""
    outside = tmp_path / "outside"
    outside.write_text("secret\n")
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.symlink_to(outside)

    with pytest.raises(ValueError, match="not a regular file"):
        target.write_atomic("clobber\n", mode=0o644)

    assert outside.read_text() == "secret\n"
    assert target.path.is_symlink()
    # No temp file was made before the refusal.
    assert [p.name for p in target.path.parent.iterdir()] == ["leaf"]


def test_write_atomic_refuses_directory_leaf(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.path.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a regular file"):
        target.write_atomic("x\n", mode=0o644)
    assert target.path.is_dir()


def test_write_atomic_leaf_swapped_to_symlink_after_lstat_never_reaches_target(
    tmp_path: Path,
) -> None:
    """The lstat is not the escape boundary; ``rename(2)`` is.

    Fool the lstat into reporting a regular file while the leaf is really a
    symlink — the state a swap between the lstat and the rename produces. The
    rename replaces the directory entry and never follows the link, so the
    file it pointed at is byte-unchanged; the repo's own entry now holds ours.
    """
    outside = tmp_path / "outside"
    outside.write_text("secret\n")
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.symlink_to(outside)
    regular = outside.stat()

    def lying_lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        return regular

    with patch("quarry.safe_paths.os.lstat", side_effect=lying_lstat):
        target.write_atomic("new\n", mode=0o644)

    assert outside.read_text() == "secret\n"
    assert not target.path.is_symlink()
    assert target.path.read_text() == "new\n"


def test_is_regular_file_false_for_symlinked_leaf(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("x\n")
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.symlink_to(outside)
    assert target.is_regular_file() is False


# ── symlinked ANCESTOR must never let an op escape the repo ──────────


def test_create_exclusive_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (repo / ".punt-labs").symlink_to(external)

    with pytest.raises(ValueError, match="ancestor"):
        _target(repo).create_exclusive("x", mode=0o644)

    assert not (external / "quarry" / "leaf").exists()


def test_write_atomic_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    (external / "quarry").mkdir(parents=True)
    victim = external / "quarry" / "leaf"
    victim.write_text("KEEP\n")
    (repo / ".punt-labs").symlink_to(external)

    with pytest.raises(ValueError, match="ancestor"):
        _target(repo).write_atomic("clobber\n", mode=0o644)

    assert victim.read_text() == "KEEP\n"


def test_remove_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    (external / "quarry").mkdir(parents=True)
    planted = external / "quarry" / "leaf"
    planted.write_text("KEEP\n")
    (repo / ".punt-labs").symlink_to(external)

    with pytest.raises(ValueError, match="ancestor"):
        _target(repo).remove()

    assert planted.exists()


def test_is_regular_file_false_for_symlinked_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    (external / "quarry").mkdir(parents=True)
    (external / "quarry" / "leaf").write_text("x\n")
    (repo / ".punt-labs").symlink_to(external)
    assert _target(repo).is_regular_file() is False


def test_create_exclusive_refuses_non_directory_ancestor(tmp_path: Path) -> None:
    """A regular file where a directory ancestor is expected is refused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".punt-labs").write_text("i am a file, not a dir\n")
    with pytest.raises(ValueError, match="ancestor"):
        _target(repo).create_exclusive("x", mode=0o644)


# ── Class 1: fd/temp cleanup when os.fdopen raises ───────────────────


def test_create_exclusive_closes_fd_when_fdopen_raises(tmp_path: Path) -> None:
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def spy_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        # Record only the leaf create fd, opened O_WRONLY (not the dir walk fds).
        if flags & os.O_WRONLY:
            opened.append(fd)
        return fd

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    with (
        patch("quarry.safe_paths.os.open", side_effect=spy_open),
        patch("quarry.safe_paths.os.close", side_effect=spy_close),
        patch("quarry.safe_paths.os.fdopen", side_effect=OSError("fdopen failed")),
        pytest.raises(OSError, match="fdopen failed"),
    ):
        _target(tmp_path).create_exclusive("x", mode=0o644)

    assert opened, "expected a leaf fd to be opened"
    assert set(opened) <= set(closed), "the opened leaf fd must be closed, not leaked"


def test_write_atomic_removes_temp_when_fdopen_raises(tmp_path: Path) -> None:
    parent = tmp_path.joinpath(*_RELATIVE[:-1])
    parent.mkdir(parents=True)

    with (
        patch("quarry.safe_paths.os.fdopen", side_effect=OSError("fdopen failed")),
        pytest.raises(OSError, match="fdopen failed"),
    ):
        _target(tmp_path).write_atomic("x", mode=0o644)

    # No temp file (or leaf) is left behind after the failed atomic write.
    assert list(parent.iterdir()) == []


def test_create_exclusive_leaves_no_partial_leaf_and_retry_works(
    tmp_path: Path,
) -> None:
    """A fill failure unlinks the leaf so the next create_exclusive is not fooled.

    A stranded partial leaf would read as an existing regular file — the
    idempotent no-op — leaving a truncated marker/config on every retry.
    """
    target = _target(tmp_path)
    leaf = tmp_path.joinpath(*_RELATIVE)

    with (
        patch("quarry.safe_paths.os.fdopen", side_effect=OSError("fill failed")),
        pytest.raises(OSError, match="fill failed"),
    ):
        target.create_exclusive("CONTENT", mode=0o644)

    assert not leaf.exists()  # no partial leaf stranded

    # A real retry now creates the file with the full content, not a no-op.
    assert target.create_exclusive("CONTENT", mode=0o644) is True
    assert leaf.read_text() == "CONTENT"


def test_remove_creates_no_artifact_and_leaves_file_on_unlink_error(
    tmp_path: Path,
) -> None:
    """remove creates nothing; an unlink error propagates, leaving the file intact.

    remove has no partially-created artifact to clean (it only stats + unlinks),
    so its Class-1 obligation is simply to not corrupt state on failure.
    """
    target = _target(tmp_path)
    target.create_exclusive("keep\n", mode=0o644)
    leaf = tmp_path.joinpath(*_RELATIVE)

    with (
        patch("quarry.safe_paths.os.unlink", side_effect=OSError("unlink failed")),
        pytest.raises(OSError, match="unlink failed"),
    ):
        target.remove()

    assert leaf.read_text() == "keep\n"  # untouched, no partial state


# ── read_text: the one read primitive for repo-controlled files ──────


def test_read_text_reads_a_real_file_byte_for_byte(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.create_exclusive("k: v\r\nline2\n", mode=0o644)
    assert target.read_text() == "k: v\r\nline2\n"


def test_read_text_refuses_a_symlinked_leaf(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("secret\n")
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        target.read_text()


def test_read_text_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaf").write_text("secret\n")
    (tmp_path / ".punt-labs").mkdir()
    (tmp_path / ".punt-labs" / "quarry").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        _target(tmp_path).read_text()


def test_read_text_absent_leaf_is_file_not_found(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.path.parent.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        target.read_text()


def test_read_text_absent_ancestor_is_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _target(tmp_path).read_text()


def test_read_text_refuses_a_directory_leaf(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.path.mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        target.read_text()


def test_read_text_closes_the_fd_when_fdopen_raises(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.create_exclusive("x\n", mode=0o644)
    opened: list[int] = []
    real_open = os.open

    def spy_open(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    with (
        patch("quarry.safe_paths.os.open", side_effect=spy_open),
        patch("quarry.safe_paths.os.fdopen", side_effect=OSError("boom")),
        pytest.raises(OSError, match="boom"),
    ):
        target.read_text()
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
