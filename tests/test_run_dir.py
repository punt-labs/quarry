"""Tests for the run-directory sidecars: ``serve.port`` and ``serve.token``.

``ServeTokenFile`` carries the daemon's loopback bearer, so its writer must
satisfy the CLAUDE.md Class-1 file-I/O obligations: mode-0600 from creation,
the file descriptor closed if ``os.fdopen`` raises, and the temp file removed on
any write failure — no leaked fd, no half-written token, no world-readable
window.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Literal

import pytest

from quarry.run_dir import PortFile, RunDir, ServeTokenFile


class TestPortFile:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        pf = PortFile(tmp_path / "sub" / "serve.port")
        pf.write(12345)
        assert pf.path.read_text() == "12345"
        assert pf.read() == 12345

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        pf = PortFile(tmp_path / "a" / "b" / "serve.port")
        pf.write(8080)
        assert pf.path.exists()

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            PortFile(tmp_path / "serve.port").read()

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        pf = PortFile(tmp_path / "serve.port")
        pf.write(8420)
        pf.remove()
        pf.remove()  # missing_ok — no raise the second time
        assert not pf.path.exists()


class TestServeTokenFile:
    def test_write_creates_file_with_token(self, tmp_path: Path) -> None:
        tf = ServeTokenFile(tmp_path / "serve.token")
        tf.write("s3cr3t-token")
        assert tf.path.read_text() == "s3cr3t-token"

    def test_mode_is_0600_from_creation(self, tmp_path: Path) -> None:
        tf = ServeTokenFile(tmp_path / "serve.token")
        tf.write("token")
        mode = stat.S_IMODE(tf.path.stat().st_mode)
        assert mode == 0o600

    @pytest.mark.skipif(
        sys.platform == "win32", reason="FD_CLOEXEC/fcntl is POSIX-only"
    )
    def test_temp_fd_is_cloexec(self, tmp_path: Path) -> None:
        # The secret-write handle must be O_CLOEXEC so it is not inherited into a
        # subprocess spawned during error handling (matches the serve.lock fd).
        import fcntl  # POSIX-only; the test is skipped where it is absent

        fd = ServeTokenFile._create_0600(tmp_path / "serve.token.tmp")
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            assert flags & fcntl.FD_CLOEXEC
        finally:
            os.close(fd)

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        tf = ServeTokenFile(tmp_path / "a" / "b" / "serve.token")
        tf.write("token")
        assert tf.path.exists()

    def test_read_round_trips_and_strips(self, tmp_path: Path) -> None:
        tf = ServeTokenFile(tmp_path / "serve.token")
        tf.write("token")
        assert tf.read() == "token"

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        """A missing token means no daemon owns the run dir — fail loud."""
        with pytest.raises(FileNotFoundError):
            ServeTokenFile(tmp_path / "serve.token").read()

    def test_fdopen_raise_closes_fd_and_removes_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``os.fdopen`` raises it never took ownership of the fd.

        The writer must close the fd itself (no descriptor leak) and clear the
        temp file (no orphan).  We record every fd ``os.open`` hands out and
        every fd ``os.close`` receives, then assert the opened fd was closed.
        """
        opened: list[int] = []
        closed: list[int] = []
        real_open = os.open
        real_close = os.close

        def tracking_open(path: str, flags: int, mode: int = 0o777) -> int:
            fd = real_open(path, flags, mode)
            opened.append(fd)
            return fd

        def tracking_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        def boom_fdopen(*_args: object, **_kwargs: object) -> object:
            raise OSError("fdopen failed")

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "close", tracking_close)
        monkeypatch.setattr(os, "fdopen", boom_fdopen)

        token_path = tmp_path / "serve.token"
        with pytest.raises(OSError, match="fdopen failed"):
            ServeTokenFile(token_path).write("token")

        assert opened, "os.open was never called"
        assert opened[0] in closed, "leaked fd — os.close was not called on it"
        assert not token_path.with_name("serve.token.tmp").exists(), (
            "temp file left behind"
        )
        assert not token_path.exists(), "destination should not exist on failure"

    def test_write_failure_removes_tmp_and_leaves_no_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure during write clears the temp file and never writes the dest."""

        class BoomWriter:
            def __enter__(self) -> BoomWriter:
                return self

            def __exit__(self, *_exc: object) -> Literal[False]:
                return False

            def write(self, _data: str) -> int:
                raise OSError("disk full")

        monkeypatch.setattr(os, "fdopen", lambda *_a, **_k: BoomWriter())

        token_path = tmp_path / "serve.token"
        with pytest.raises(OSError, match="disk full"):
            ServeTokenFile(token_path).write("token")

        assert not token_path.with_name("serve.token.tmp").exists()
        assert not token_path.exists()

    def test_overwrite_replaces_atomically(self, tmp_path: Path) -> None:
        tf = ServeTokenFile(tmp_path / "serve.token")
        tf.write("old-token")
        tf.write("new-token")
        assert tf.read() == "new-token"
        assert stat.S_IMODE(tf.path.stat().st_mode) == 0o600

    def test_token_temp_0600_despite_world_readable_leftover(
        self, tmp_path: Path
    ) -> None:
        """A stale, world-readable temp must not leak the token's mode.

        ``O_EXCL`` refuses to reuse the leftover; the writer unlinks it and
        recreates 0600, so the secret is never written into a 0644 file.
        """
        token_path = tmp_path / "serve.token"
        stale = token_path.with_name("serve.token.tmp")
        stale.write_text("junk")
        stale.chmod(0o644)
        ServeTokenFile(token_path).write("token")
        assert token_path.read_text() == "token"
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert not stale.exists(), "stale temp not consumed by the atomic rename"

    def test_port_write_leaves_token_temp_untouched(self, tmp_path: Path) -> None:
        """Distinct temp paths: the port writer never touches the token's temp.

        Both sidecars once collapsed to a shared ``serve.tmp`` via
        ``with_suffix(".tmp")``; a port write would then clobber a token temp
        (and leave the token's temp world-readable for the next token write to
        inherit).  Each now stages through its own name, so a token-temp
        leftover survives a port write untouched.
        """
        token_temp = (tmp_path / "serve.token").with_name("serve.token.tmp")
        token_temp.write_text("leftover")
        token_temp.chmod(0o644)
        PortFile(tmp_path / "serve.port").write(8420)
        assert token_temp.exists(), "port write used the token's temp path"
        assert stat.S_IMODE(token_temp.stat().st_mode) == 0o644


class TestRunDir:
    def test_sidecar_paths_sit_beside_data_dir(self, tmp_path: Path) -> None:
        rd = RunDir(tmp_path)
        assert rd.port_file.path == tmp_path / "serve.port"
        assert rd.token_file.path == tmp_path / "serve.token"

    def test_files_round_trip_through_run_dir(self, tmp_path: Path) -> None:
        rd = RunDir(tmp_path)
        rd.port_file.write(8420)
        rd.token_file.write("token")
        assert rd.port_file.read() == 8420
        assert rd.token_file.read() == "token"
