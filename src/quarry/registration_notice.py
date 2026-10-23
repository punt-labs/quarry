"""Register a directory for a SessionStart, or say why the registry refused."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from quarry.refusal import RegistrationRefusedError
from quarry.session_start_templates import SessionStartTemplates

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.sync_registry import SyncRegistry

logger = logging.getLogger(__name__)


@final
class RegistrationNotice:
    """Auto-register a directory, or return the context that states the refusal.

    The refusal's text is fixed and name-free, so it is safe to show; its detail
    (which collection, which directory) goes to the redacted log line.  A refusal
    says nothing about the daemon, so the trailer still applies.
    """

    __slots__ = ()

    @staticmethod
    def register(
        registry: SyncRegistry, directory: Path, collection: str
    ) -> str | None:
        """Register *directory*; return the refusal context, or ``None`` on success."""
        try:
            registry.register_directory(directory, collection)
        except RegistrationRefusedError as exc:
            logger.warning(
                "session-start: auto-register of %s refused: %s [%s]",
                directory,
                exc,
                exc.detail,
            )
            header = (
                f"Quarry: auto-register of {directory} was refused: {exc}.\n"
                "Nothing was registered; run 'quarry enable' after resolving it."
            )
            return f"{header}\n{SessionStartTemplates.trailer()}"
        return None
