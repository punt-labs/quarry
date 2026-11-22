"""Read-capture admission checks for the post-Read hook."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from quarry.scrub import Scrubber, ScrubConfig

if TYPE_CHECKING:
    from quarry.collection_resolver import CollectionResolver
    from quarry.config import Settings

# Secrets-only scrubber, built once: a client-side defense-in-depth check on
# Read content ahead of the daemon's own scrub, relevant when QUARRY_URL
# points at a remote daemon.  PII/profanity passes are off — those aren't
# reasons to withhold a capture, only genuine secrets are.
_SECRET_SCRUBBER = Scrubber(ScrubConfig(scrub_pii=False, scrub_profanity=False))


@final
class ReadAdmission:
    """Read-hook admission helpers grouped by shared vocabulary.

    Settings load, resolver build, and client-side secret pre-scanning live
    here so the post-Read handler stays focused on orchestration.
    """

    __slots__ = ()

    @staticmethod
    def content_has_secret(content: str) -> bool:
        """Return whether *content* matches a secret pattern (PEM/env/PAT/etc).

        Defense-in-depth ahead of the daemon's own scrub — catches an
        obvious secret before it leaves the machine, which matters when
        ``QUARRY_URL`` points at a remote daemon.
        """
        _, redactions = _SECRET_SCRUBBER.scrub(content)
        return bool(redactions)

    @classmethod
    def collection_resolver_for(cls, cwd: str) -> CollectionResolver | None:
        """Open a :class:`CollectionResolver`, or ``None`` on failure.

        Read fires often; a settings-load failure must never crash the
        handler.  ``None`` is the "no in-tree exclusion possible"
        contract that :class:`ReadCaptureFilter` treats as "resolver
        unavailable" — the first admission check skips, the remaining
        three still run.
        """
        del cwd  # resolver is settings-scoped; cwd flows to should_capture
        try:
            settings = cls._resolve_settings()
        except (OSError, ValueError):
            return None
        try:
            from quarry.collection_resolver import CollectionResolver  # noqa: PLC0415
            from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

            conn = SyncRegistry(settings.registry_path)
        except (OSError, ValueError):
            return None
        return CollectionResolver(conn)

    @staticmethod
    def _resolve_settings() -> Settings:
        """Load settings resolved for the default database."""
        from quarry.config import Settings  # noqa: PLC0415

        return Settings.load().resolve_db_paths(None)
