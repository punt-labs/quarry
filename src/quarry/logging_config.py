"""Logging configuration for punt-quarry."""

from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path

_LOG_DIR = Path.home() / ".punt-labs" / "quarry" / "logs"
_LOG_FILE = _LOG_DIR / "quarry.log"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MAX_BYTES = 5_242_880  # 5 MB
_BACKUP_COUNT = 5


def configure_logging(*, stderr_level: str = "WARNING") -> None:
    """Configure logging with rotating file and stderr handlers.

    File handler is always active at INFO level.
    Stderr handler level is controlled by the caller, unless overridden
    by the ``QUARRY_LOG_LEVEL`` environment variable.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    env_level = os.environ.get("QUARRY_LOG_LEVEL", "").upper()
    valid_levels = logging.getLevelNamesMapping()
    if env_level and env_level in valid_levels:
        effective_level = env_level
    else:
        effective_level = stderr_level

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": _FORMAT,
                    "datefmt": _DATE_FORMAT,
                },
            },
            "handlers": {
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": str(_LOG_FILE),
                    "maxBytes": _MAX_BYTES,
                    "backupCount": _BACKUP_COUNT,
                    "encoding": "utf-8",
                    "formatter": "standard",
                    "level": "INFO",
                },
                "stderr": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "standard",
                    "level": effective_level,
                },
            },
            "loggers": {
                "lancedb": {"level": "WARNING"},
                "onnxruntime": {"level": "WARNING"},
                "httpx": {"level": "WARNING"},
            },
            "root": {
                "level": "DEBUG",
                "handlers": ["file", "stderr"],
            },
        }
    )
