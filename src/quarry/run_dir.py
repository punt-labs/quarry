"""Run-directory sidecars: the daemon's ``serve.port`` and ``serve.token``.

Both files live beside the LanceDB data dir so a client can discover the running
daemon's bound port and present its loopback bearer.  ``serve.token`` is the
credential that closes the loopback-authentication gap (DES-031 v2.2 R4): before
it, any local UID could reach the unauthenticated daemon on ``127.0.0.1`` and
read, poison, or delete the whole index.  The token is written mode-0600 *from
creation* via an atomic ``os.open`` (``O_EXCL``) + tmp-rename, so neither a
world-readable window (a create-then-chmod race, or reusing a stale temp whose
mode we do not control) nor a partial-file window (a crash mid-write) can ever
expose or corrupt it.  Each sidecar stages through its *own* temp name
(``serve.port.tmp`` / ``serve.token.tmp``) so the port writer can never leave a
world-readable temp that the token writer would inherit.

The module is deliberately engine-free: both the daemon (writer) and the client
tier (reader) import it, so it must not pull in ``quarry.db`` or embeddings.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class PortFile:
    """The ``serve.port`` sidecar: the daemon's actual bound port for callers.

    The port is not a secret — ``port=0`` callers need it to find the
    OS-assigned ephemeral port — so it is written in the clear with a plain
    text write rather than the atomic 0600 dance ``ServeTokenFile`` uses.
    """

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        return self._path

    def write(self, port: int) -> None:
        """Write the bound port atomically (tmp + rename).

        Atomic so a failed write leaves no partial file at the destination:
        the daemon's startup-cleanup removes only sidecars it fully wrote, so
        a half-written port must not linger to be mistaken for a live daemon.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Own temp name (serve.port.tmp) — never the token's, so a leftover
        # port temp can never be inherited by the 0600 token writer.
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(str(port))
            tmp.replace(self._path)
        except BaseException:
            # Remove the temp on ANY interruption (KeyboardInterrupt/SystemExit
            # too), not just OSError — the temp is a pure implementation detail
            # and must never linger.  Mirrors ServeTokenFile.write's cleanup.
            tmp.unlink(missing_ok=True)
            raise
        logger.info("Wrote port file: %s (port %d)", self._path, port)

    def read(self) -> int:
        """Return the recorded bound port; raise ``FileNotFoundError`` if absent.

        Absence means the daemon is not running (or has not bound yet).  The
        caller decides how to surface that — this reader never invents a
        default port, which would silently point a client at the wrong daemon.
        """
        return int(self._path.read_text().strip())

    def remove(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
            logger.info("Removed port file: %s", self._path)
        except OSError:
            logger.warning("Could not remove port file: %s", self._path)


@final
class ServeTokenFile:
    """The ``serve.token`` sidecar: the daemon's loopback bearer, mode-0600.

    Threat model: a co-tenant local user on a multi-user host.  The daemon
    generates this token at startup and gates every loopback request on it;
    only a process running as the daemon's UID can read the 0600 file, so a
    different local UID cannot obtain the bearer.  The write is atomic and the
    mode is set at creation (never chmod-after) so the token is never briefly
    world-readable and never left half-written at the destination path.
    """

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        return self._path

    def write(self, token: str) -> None:
        """Write *token* mode-0600, atomically (create 0600 → write → rename).

        The temp is created ``O_EXCL`` so the token is never written into a
        pre-existing file whose mode we do not control: ``O_TRUNC`` would reuse
        a stale temp (a crashed write's leftover, or another sidecar's
        world-readable temp) and reset only its *contents*, not its mode,
        briefly exposing the secret.  Its temp name is its own (serve.token.tmp)
        so it can never collide with the port sidecar's temp.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = self._create_0600(tmp)
        # If fdopen fails it has NOT taken ownership of the fd, so we must close
        # it ourselves and clear the temp file — otherwise both leak.
        try:
            f = os.fdopen(fd, "w")
        except BaseException:
            os.close(fd)
            tmp.unlink(missing_ok=True)
            raise
        # The rename lives inside the try so any write/replace failure removes
        # the temp file rather than leaving a partial token on disk.
        try:
            with f:
                f.write(token)
            tmp.replace(self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        logger.info("Wrote serve token file: %s", self._path)

    @staticmethod
    def _create_0600(tmp: Path) -> int:
        """Create *tmp* 0600 with ``O_EXCL``; unlink a stale leftover and retry once.

        ``O_EXCL`` guarantees a fresh 0600 file — the token is never written into
        an existing temp whose mode we do not control.  A leftover from a crashed
        write is unlinked and the create retried once; a second ``FileExistsError``
        (a racing writer) propagates, failing closed rather than reusing the file.
        """
        # O_CLOEXEC: the secret-write handle must not be inherited into a
        # subprocess spawned during error handling (matches the serve.lock fd).
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        try:
            return os.open(str(tmp), flags, 0o600)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            return os.open(str(tmp), flags, 0o600)

    def read(self) -> str:
        """Return the token; raise ``FileNotFoundError`` if the daemon is down.

        The reader never returns an empty or default token: a missing file
        means no daemon owns this run dir, and a client must fail loud rather
        than send an empty bearer that would be rejected far from its cause.
        """
        return self._path.read_text().strip()

    def remove(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
            logger.info("Removed serve token file: %s", self._path)
        except OSError:
            logger.warning("Could not remove serve token file: %s", self._path)


@final
class RunDir:
    """The daemon's run directory: home of the ``serve.port``/``serve.token`` pair.

    Both sidecars sit beside the LanceDB data dir (``lancedb_path.parent``), so
    one ``RunDir`` built from that directory hands out both files and keeps the
    naming in a single place instead of scattered path joins.
    """

    _data_dir: Path

    def __new__(cls, data_dir: Path) -> Self:
        self = super().__new__(cls)
        self._data_dir = data_dir
        return self

    @property
    def port_file(self) -> PortFile:
        return PortFile(self._data_dir / "serve.port")

    @property
    def token_file(self) -> ServeTokenFile:
        return ServeTokenFile(self._data_dir / "serve.token")

    @property
    def lock_path(self) -> Path:
        """Path to ``serve.lock`` — the daemon's exclusive-ownership advisory lock.

        NOT a client-read sidecar: clients never open it.  The daemon holds an
        exclusive ``flock`` on it for its whole lifetime so exactly one daemon
        owns a run dir, making that daemon the SOLE writer of serve.token /
        serve.port.  That structurally closes the shared-run-dir clobber (a
        second daemon on a different port overwriting the first's token) and the
        token-writer's temp-retry race (no concurrent writer can exist).
        """
        return self._data_dir / "serve.lock"
