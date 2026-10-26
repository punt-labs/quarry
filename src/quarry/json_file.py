"""Read a JSON object from a config file we do not own, refusing anything else."""

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING, ClassVar, final

from quarry.doctor_failure import CheckFailure

if TYPE_CHECKING:
    from pathlib import Path


class ConfigError(ValueError):
    """A config file we cannot use; the message says why and names the file."""


@final
class JsonFile:
    """Read a JSON object from a file we do not own, refusing anything else.

    Every way the file can be wrong is a :class:`ConfigError` whose message names
    the file and says why: not a regular file (a FIFO would hang a read, a device
    is not a config), too large, not UTF-8, not JSON (including nesting too deep
    to parse), or not a JSON object.  An empty file is an empty object.  The file
    is opened once, without blocking, and checked with ``fstat`` on that
    descriptor, so what is checked is what is read.
    """

    # A real config is kilobytes; a cap this far above it still keeps a hostile
    # file from exhausting memory in the JSON parser.
    MAX_BYTES: ClassVar[int] = 1_048_576

    __slots__ = ()

    @classmethod
    def load(cls, path: Path) -> dict[str, object]:
        """Return the object in *path*, or raise :class:`ConfigError` saying why."""
        raw = cls._read(path)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            msg = f"{path.name}: not UTF-8"
            raise ConfigError(msg) from exc
        except (json.JSONDecodeError, RecursionError, MemoryError) as exc:
            msg = f"{path.name}: not JSON"
            raise ConfigError(msg) from exc
        if not isinstance(data, dict):
            msg = f"{path.name}: not a JSON object"
            raise ConfigError(msg)
        return data

    @classmethod
    def object_at(
        cls, parent: dict[str, object], key: str, source: Path
    ) -> dict[str, object]:
        """Return ``parent[key]`` as an object (empty if absent), else raise."""
        found = parent.get(key, {})
        if not isinstance(found, dict):
            msg = f"{source.name}: {key} is not an object"
            raise ConfigError(msg)
        return found

    @staticmethod
    def why(exc: OSError | ValueError) -> str:
        """Return the reason to report: ours as written, anything else by class."""
        return str(exc) if isinstance(exc, ConfigError) else CheckFailure.detail(exc)

    @classmethod
    def _read(cls, path: Path) -> bytes:
        """Return at most the cap's worth of bytes of a regular file."""
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                msg = f"{path.name}: not a regular file"
                raise ConfigError(msg)
            if info.st_size > cls.MAX_BYTES:
                msg = f"{path.name}: larger than {cls.MAX_BYTES} bytes"
                raise ConfigError(msg)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                return handle.read(cls.MAX_BYTES + 1)
        finally:
            os.close(fd)
