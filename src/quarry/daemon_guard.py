"""Tell whether a quarryd daemon owns a run directory, and hold it exclusively."""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

from quarry.daemon_ca_error import DaemonCaError
from quarry.daemon_unready_error import DaemonUnreadyError
from quarry.doctor_health_probe import HealthProbe

if TYPE_CHECKING:
    from collections.abc import Generator

    from quarry.run_dir import RunDir

_fcntl = importlib.import_module("fcntl")


@final
class DaemonGuard:
    """Decide whether a daemon is running for a run dir; keep a new one out.

    A daemon is running when it holds the run dir's ``serve.lock``, or when the
    port in ``serve.port`` answers ``/health``.  A port file alone is not proof:
    one outlives a crashed daemon, so the port is probed.  A port that answers
    with a certificate fault is something alive, so it counts as running.
    """

    __slots__ = ("_run_dir",)

    _run_dir: RunDir

    def __new__(cls, run_dir: RunDir) -> Self:
        self = super().__new__(cls)
        self._run_dir = run_dir
        return self

    def is_running(self) -> bool:
        """Return whether a daemon owns the run dir; never creates a file."""
        return self._lock_is_held() or self._port_answers()

    @contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold the run dir's daemon lock, so no daemon can start meanwhile.

        Raises ``ValueError`` when a daemon already holds it.
        """
        path = self._run_dir.lock_path
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

    def _lock_is_held(self) -> bool:
        path = self._run_dir.lock_path
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def _port_answers(self) -> bool:
        try:
            port = self._run_dir.port_file.read()
        except (OSError, ValueError):
            return False
        try:
            return HealthProbe.fetch(port) is not None
        except (DaemonCaError, DaemonUnreadyError):
            return True
