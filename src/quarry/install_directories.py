"""The data and logs directories `quarry install` creates, and how it says so."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, final

from quarry.config import Settings
from quarry.db_pointer import SELECTION
from quarry.doctor_failure import CheckFailure
from quarry.logging_config import LoggingConfig
from quarry.results import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@final
class InstallDirectories:
    """Locate and create the selected database's directory and the log directory."""

    __slots__ = ()

    @staticmethod
    def data_dir() -> Path:
        """Return the selected database's directory, as the daemon checks read it."""
        return Settings().resolve_db_paths(SELECTION.active()).lancedb_path

    @classmethod
    def check(cls) -> CheckResult:
        """Report whether the selected database's directory exists and is writable."""
        data_dir = cls.data_dir()
        is_usable = data_dir.exists() and os.access(data_dir, os.W_OK)
        if is_usable:
            message = str(data_dir)
        elif data_dir.exists():
            message = f"{data_dir} exists but is not writable"
        else:
            message = f"{data_dir} does not exist (run 'quarry install')"
        return CheckResult(name="Data directory", passed=is_usable, message=message)

    @classmethod
    def create(cls) -> bool:
        """Create the data and logs directories; return whether both now exist.

        Each is located and made on its own, so one that cannot be does not hide
        the other, and each failure names the directory it is about.
        """
        results = [
            cls._make("data", cls.data_dir),
            cls._make("logs", LoggingConfig.log_dir),
        ]
        return all(results)

    @staticmethod
    def _make(label: str, locate: Callable[[], Path]) -> bool:
        """Locate and create one directory; say how it went."""
        try:
            directory = locate()
            directory.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError, ValueError) as exc:
            why = CheckFailure.detail(exc)
            print(f"  ✗ Failed to create the {label} directory: {why}")  # noqa: T201
            return False
        print(f"  ✓ {directory}")  # noqa: T201
        return True
