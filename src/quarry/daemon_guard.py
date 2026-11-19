"""Tell whether a quarryd daemon owns any run directory, and hold them all."""

from __future__ import annotations

import importlib
import os
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Self, final

from quarry.daemon_ca_error import DaemonCaError
from quarry.daemon_unready_error import DaemonUnreadyError
from quarry.database_roster import DatabaseRoster
from quarry.doctor_health_probe import HealthProbe
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_fcntl = importlib.import_module("fcntl")


@final
class DaemonGuard:
    """Decide whether a daemon is running for a set of run dirs; keep one out.

    A daemon started for one database watches every database in its roster, so
    it can write to a sibling's tables.  :meth:`roster` therefore guards the
    target run dir plus every sibling that holds a ``registry.db``, the same set
    the daemon's watch roster opens.

    A run dir is owned when its ``serve.lock`` is held, or when the port in
    ``serve.port`` answers ``/health``.  A port file alone is not proof: one
    outlives a crashed daemon, so the port is probed.  A port that answers with
    a certificate fault is something alive, so it counts as running.
    """

    __slots__ = ("_run_dirs",)

    _run_dirs: tuple[RunDir, ...]

    def __new__(cls, run_dirs: tuple[RunDir, ...]) -> Self:
        """Select daemon run directories whose ownership locks must be checked."""
        self = super().__new__(cls)
        self._run_dirs = run_dirs
        return self

    @classmethod
    def roster(cls, target: Path, quarry_root: Path) -> Self:
        """Return the guard over *target* and every database a daemon could hold.

        That is every roster member plus any directory with a daemon's lock or
        port file, so a daemon booted on a database with no registry yet is seen.
        """
        names = DatabaseRoster(quarry_root).with_daemon_traces()
        dirs = {target, *(quarry_root / name for name in names)}
        return cls(tuple(RunDir(d) for d in sorted(dirs)))

    def is_running(self) -> bool:
        """Return whether a daemon owns any guarded run dir; creates no file."""
        return any(
            self._lock_is_held(d) or self._port_answers(d) for d in self._run_dirs
        )

    @contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold every guarded run dir's daemon lock, so none can start meanwhile.

        Locks are taken in a fixed (sorted) order so two concurrent repairs
        cannot deadlock.  Raises ``ValueError`` when a daemon holds any of them,
        or answers on a recorded port; a refusal creates no lock file.
        """
        for run_dir in self._run_dirs:
            if self._port_answers(run_dir):
                where = run_dir.lock_path.parent
                msg = f"a quarryd daemon answers for {where}; stop it first"
                raise ValueError(msg)
        with ExitStack() as stack:
            for run_dir in self._run_dirs:
                stack.enter_context(self._hold(run_dir))
            yield

    @staticmethod
    @contextmanager
    def _hold(run_dir: RunDir) -> Generator[None]:
        path = run_dir.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except BlockingIOError as exc:
                msg = f"a quarryd daemon owns {path.parent}; stop it first"
                raise ValueError(msg) from exc
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _lock_is_held(run_dir: RunDir) -> bool:
        try:
            fd = os.open(str(run_dir.lock_path), os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    @staticmethod
    def _port_answers(run_dir: RunDir) -> bool:
        try:
            port = run_dir.port_file.read()
        except (OSError, ValueError):
            return False
        try:
            return HealthProbe.fetch(port) is not None
        except (DaemonCaError, DaemonUnreadyError):
            return True
