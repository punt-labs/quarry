"""The data and logs directories `quarry install` creates, and how it says so."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from quarry.config import Settings
from quarry.db_pointer import SELECTION
from quarry.doctor_failure import CheckFailure
from quarry.logging_config import LoggingConfig

if TYPE_CHECKING:
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
    def create(cls) -> bool:
        """Create the data and logs directories; return whether both now exist.

        Each failure names the directory it is about, so the report says which of
        the two to look at.
        """
        try:
            directories = (("data", cls.data_dir()), ("logs", LoggingConfig.log_dir()))
        except (OSError, RuntimeError, ValueError) as exc:
            why = CheckFailure.detail(exc)
            print(f"  ✗ Failed to locate the data and logs directories: {why}")  # noqa: T201
            return False
        for label, directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                why = CheckFailure.detail(exc)
                print(f"  ✗ Failed to create the {label} directory {directory}: {why}")  # noqa: T201
                return False
            print(f"  ✓ {directory}")  # noqa: T201
        return True
