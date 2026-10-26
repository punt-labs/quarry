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
    def data_dir(settings: Settings) -> Path:
        """Return the selected database's directory, as the daemon checks read it."""
        return settings.resolve_db_paths(SELECTION.active()).lancedb_path

    @classmethod
    def check(cls, settings: Settings) -> CheckResult:
        """Report whether the selected database's directory exists and is writable."""
        data_dir = cls.data_dir(settings)
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
        the other. A failure to locate a directory names which one; a failure to
        create it names its path too.
        """
        results = [
            cls._make("data", lambda: cls.data_dir(Settings())),
            cls._make("logs", LoggingConfig.log_dir),
        ]
        return all(results)

    @staticmethod
    def _make(label: str, locate: Callable[[], Path]) -> bool:
        """Locate and create one directory; say how each step went."""
        try:
            directory = locate()
        except (OSError, RuntimeError, ValueError) as exc:
            failure = (
                f"Failed to locate the {label} directory: {CheckFailure.detail(exc)}"
            )
        else:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                why = CheckFailure.detail(exc)
                failure = f"Failed to create the {label} directory {directory}: {why}"
            else:
                print(f"  ✓ {directory}")  # noqa: T201
                return True
        print(f"  ✗ {failure}")  # noqa: T201
        return False
