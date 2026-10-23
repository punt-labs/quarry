"""Hook handlers for Claude Code integration.

Each handler receives the parsed stdin JSON from Claude Code and returns
a dict that will be serialized to stdout.  All handlers are fail-open:
exceptions are logged but never propagated to the caller.

Heavy imports (lancedb, pydantic, onnxruntime) are deferred to the
handler functions that actually need them.  The module-level imports
are stdlib-only so that ``quarry-hook`` can load this module without
paying the full dependency tax.

Hook events:
    session-start    — SessionStart: auto-register and sync the current repo.
    post-web-fetch   — PostToolUse on WebFetch: auto-ingest fetched URLs.
    pre-compact      — PreCompact: ingest the pre-compaction conversation transcript.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry._hook_trace import HookPayload, HookTrace
from quarry._stdlib import load_hook_config
from quarry.active_session_context import ActiveSessionContext
from quarry.pre_compact_hook import PreCompactHook
from quarry.session_start_reply import SessionStartReply

if TYPE_CHECKING:
    from quarry.collection_resolver import CollectionResolver
    from quarry.config import Settings
    from quarry.sync_registry import DirectoryRegistration, SyncRegistry

logger = logging.getLogger(__name__)


def handle_session_start(payload: dict[str, object]) -> dict[str, object]:
    """Handle SessionStart hook.

    Gates on the ``.punt-labs/quarry/enabled`` marker (§ 2.11): only a repo
    that opted in through ``quarry enable`` gets the active flow (walk-up
    coverage → auto-register → background sync → active context). A repo
    without the marker gets one of two read-only nudges — the state machine
    is:

    "Marker present" means at *cwd* itself OR at the root of the registration
    covering *cwd* — a session opened in a subdirectory of an enabled repo
    reads the marker the repo's own root carries, not one at the subdirectory.

    * marker present → Path A (active). No coverage becomes auto-register;
      a child of a parent registration reuses the parent; a subsumption is
      refused; a down daemon defers.
    * marker absent, no covering registration → Path B (nudge to
      ``quarry enable``). The registry is read but never mutated.
    * marker absent, covering registration exists → Path C (surface the
      drift). Names both ``quarry enable`` and ``quarry deregister``; the
      registry is never mutated because neither door is safe to pick
      automatically.
    """
    trace = HookTrace("session-start")
    cwd = HookPayload.as_dir(payload.get("cwd"))
    if not cwd:
        trace.skip("cwd")
        return {}
    on = load_hook_config(cwd).session_sync
    trace.mark_config(on=on)
    if not on:
        trace.skip("config")
        return {}
    directory = Path(cwd).resolve()
    if not directory.is_dir():
        logger.warning("session-start: cwd is not a directory: %s", directory)
        trace.skip("not-a-dir")
        return {}
    trace.mark_payload(ok=True)
    return _SessionStartContext.open(directory).dispatch().traced(trace)


@final
class _SessionStartContext:
    """Own the cwd + registry state and route the SessionStart marker gate.

    Instantiated once per session-start invocation; ``dispatch()`` picks the
    Path A / B / C handler from the marker present/absent by covering
    registration axes and closes the registry connection on the way out.
    """

    __slots__ = ("_conn", "_directory", "_resolver")

    _directory: Path
    _conn: SyncRegistry
    _resolver: CollectionResolver

    def __new__(
        cls, directory: Path, conn: SyncRegistry, resolver: CollectionResolver
    ) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._conn = conn
        self._resolver = resolver
        return self

    @classmethod
    def open(cls, directory: Path) -> Self:
        """Build the context with a fresh registry connection."""
        from quarry.collection_resolver import CollectionResolver  # noqa: PLC0415
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        settings = cls._resolve_settings()
        conn = SyncRegistry(settings.registry_path)
        return cls(directory, conn, CollectionResolver(conn))

    def dispatch(self) -> SessionStartReply:
        """Route to Path A / B / C by (marker, covering) and close the registry."""
        try:
            registration = self._resolver.covering_registration(str(self._directory))
            collection = registration.collection if registration is not None else None
            if self._marker_present(registration):
                return self._path_a(collection)
            if collection is not None:
                return self._path_c(collection)
            return self._path_b()
        finally:
            self._conn.close()

    def _marker_present(self, registration: DirectoryRegistration | None) -> bool:
        """Return whether the marker is present at cwd OR at the covering root.

        A session opened in a subdirectory of an enabled repo has no marker of
        its own — the marker lives at the repo root the registration names —
        so ancestry, not just cwd, decides "enabled."
        """
        from quarry.enabled_marker import EnabledMarker  # noqa: PLC0415

        if EnabledMarker(self._directory).is_present():
            return True
        return (
            registration is not None
            and EnabledMarker(Path(registration.directory)).is_present()
        )

    def _path_a(self, collection: str | None) -> SessionStartReply:
        """Marker present: run the active flow (auto-register if needed, sync)."""
        if collection is None:
            registered_or_short_circuit = self._auto_register()
            if isinstance(registered_or_short_circuit, SessionStartReply):
                return registered_or_short_circuit
            collection = registered_or_short_circuit
        return SessionStartReply.of(
            ActiveSessionContext(self._directory, collection).text()
        )

    def _path_b(self) -> SessionStartReply:
        """No marker, no coverage: nudge the operator to run ``quarry enable``.

        Read-only: the covering registration was already checked to reach
        here, but nothing is written — the daemon is not consulted, no
        registration is written, no guide is deposited. § 2.3 reserves both
        mutations to the explicit ``enable`` verb.
        """
        return SessionStartReply.nudge_enable(self._directory)

    def _path_c(self, collection: str) -> SessionStartReply:
        """Marker absent + coverage exists: surface the drift; two doors, no auto-fix.

        Auto-register is refused (already registered); auto-deregister is
        refused (would delete indexed data on marker drift, violating § 2.9's
        promise that toggling never destroys committed content).
        """
        logger.warning(
            "session-start: covering registration %r for %s has no opt-in "
            "marker; surfacing drift instead of auto-registering",
            collection,
            self._directory,
        )
        return SessionStartReply.drift_surface(self._directory, collection)

    def _auto_register(self) -> str | SessionStartReply:
        """Register the covering row and return its collection name.

        Returns the collection ``str`` on success. Returns the reply when the
        flow must short-circuit — subsumption refusal, a registration the
        registry refuses, or a daemon-unreachable defer — each traced as what it
        is, never as a capture.
        """
        if self._conn.has_registrations_under(self._directory):
            logger.warning(
                "session-start: existing child registrations found under %s; "
                "skipping auto-register to prevent subsumption. Run "
                "'quarry enable %s' to explicitly register the parent.",
                self._directory,
                self._directory,
            )
            return SessionStartReply.subsumption(self._directory)
        # A same-directory re-adopt reuses this cwd's own keep-data archive
        # before consulting the daemon — so a re-open of an archived repo
        # works even when quarryd is down.
        collection = self._resolver.archived_collection_for(self._directory)
        if collection is None:
            try:
                chunk_collections = self._daemon_chunk_collections()
            except ConnectionError:
                # Fail closed on the merge-safety front: an unverifiable chunk set
                # means a fresh name is unpickable, and writing a registration now
                # would clear the orphan sweep's pending mark and arm a latent
                # cross-project merge on the daemon's return.  Per R2b the emitted
                # context still carries the trigger rules so the agent can act on
                # the diagnosis.
                logger.warning(
                    "session-start: quarryd unreachable; deferring "
                    "auto-registration of %s to avoid a cross-project merge",
                    self._directory,
                )
                return SessionStartReply.daemon_unreachable(self._directory)
            collection = self._resolver.unique_collection_name(
                self._directory, chunk_collections
            )
        refused = SessionStartReply.refusing(self._conn, self._directory, collection)
        if refused is not None:
            return refused
        logger.info(
            "session-start: auto-registered %s as '%s'",
            self._directory,
            collection,
        )
        return collection

    @staticmethod
    def _resolve_settings() -> Settings:
        """Load settings resolved for the default database."""
        from quarry.config import Settings  # noqa: PLC0415

        return Settings.load().resolve_db_paths(None)

    @staticmethod
    def _daemon_chunk_collections() -> frozenset[str]:
        """Return the daemon's chunk-bearing collection names.

        Raise ``ConnectionError`` when the daemon is unreachable or the client is
        misconfigured: a down daemon yields no listing, and an empty set from a down
        daemon is indistinguishable from a genuinely empty catalog.  Translating the
        client-specific errors into one boundary-neutral exception here lets the
        caller fail CLOSED without importing quarry.client's exception hierarchy — a
        fresh name picked against an unverifiable (empty) chunk set would arm a latent
        cross-project chunk merge.
        """
        from quarry.client import (  # noqa: PLC0415
            ClientConfigError,
            QuarryError,
            TargetResolver,
        )

        try:
            listing = TargetResolver.connect().list_registrations()
        except (ClientConfigError, QuarryError) as exc:
            msg = "quarryd unreachable; chunk-collection set unverifiable"
            raise ConnectionError(msg) from exc
        return frozenset(listing.chunk_collections)


def handle_post_web_fetch(payload: dict[str, object]) -> dict[str, object]:
    """Handle PostToolUse on WebFetch: capture the fetched page, fail-open.

    See :class:`~quarry.web_fetch_hook.WebFetchHook`.
    """
    from quarry.web_fetch_hook import WebFetchHook  # noqa: PLC0415

    return WebFetchHook(payload).run()


def handle_pre_compact(payload: dict[str, object]) -> dict[str, object]:
    """Handle PreCompact hook.

    See :class:`~quarry.pre_compact_hook.PreCompactHook`.
    """
    return PreCompactHook(payload).run()
