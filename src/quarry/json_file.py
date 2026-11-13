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
    def load(cls, path: Path, *, max_bytes: int = MAX_BYTES) -> dict[str, object]:
        """Return the object in *path*, or raise :class:`ConfigError` saying why.

        *max_bytes* bounds how much of *path* is read before it is refused;
        the default is the 1 MiB untrusted-input cap. Pass a larger bound only
        for a file this process already trusts by convention (e.g. Claude
        Code's own unbounded-growth ``~/.claude.json``) -- every other caller
        keeps the default cap unchanged.
        """
        try:
            raw = cls._read(path, max_bytes)
        except OSError as exc:
            msg = f"{path.name}: {CheckFailure.detail(exc)}"
            raise ConfigError(msg) from exc
        return cls._parsed(raw, path)

    @classmethod
    def _parsed(cls, raw: bytes, path: Path) -> dict[str, object]:
        """Return *raw*'s decoded JSON object, or raise :class:`ConfigError`."""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            msg = f"{path.name}: not UTF-8"
            raise ConfigError(msg) from exc
        except (
            ValueError,
            RecursionError,
            MemoryError,
        ) as exc:  # incl. JSONDecodeError
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
    def why(exc: OSError | ValueError | RecursionError, path: Path) -> str:
        """Return the reason to report, always naming *path*'s file.

        Ours are written as they are; an ``OSError`` or any other failure is shown
        by class and cause (never a path); a ``RecursionError`` writing the file
        back is the same fault as reading it.
        """
        if isinstance(exc, ConfigError):
            return str(exc)
        if isinstance(exc, RecursionError):
            return f"{path.name}: nested too deeply to rewrite"
        return f"{path.name}: {CheckFailure.detail(exc)}"

    @classmethod
    def _read(cls, path: Path, max_bytes: int) -> bytes:
        """Return a regular file's bytes, at most *max_bytes* worth.

        ``O_NOCTTY`` so a terminal device never becomes this process's controlling
        terminal; the descriptor is closed on every path.
        """
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                msg = f"{path.name}: not a regular file"
                raise ConfigError(msg)
            if info.st_size > max_bytes:
                raise cls._too_large(path, max_bytes)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(max_bytes + 1)
        finally:
            os.close(fd)
        if len(raw) > max_bytes:  # grew after fstat
            raise cls._too_large(path, max_bytes)
        return raw

    @staticmethod
    def _too_large(path: Path, max_bytes: int) -> ConfigError:
        """Return the error for a file past *max_bytes*."""
        return ConfigError(f"{path.name}: larger than {max_bytes} bytes")
