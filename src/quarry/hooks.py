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
from quarry.daemon_capture import DaemonCaptureSender
from quarry.ethos_handle import EthosConfig
from quarry.session_start_templates import SessionStartTemplates
from quarry.session_transcript import SessionTranscriptCapture
from quarry.sync_lock import SyncLock
from quarry.web_capture import WebFetchPayload

if TYPE_CHECKING:
    from quarry.collection_resolver import CollectionResolver
    from quarry.config import Settings
    from quarry.results import CoverageCounts
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
    result = _SessionStartContext.open(directory).dispatch()
    trace.capture()
    return result


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

    def dispatch(self) -> dict[str, object]:
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

    def _path_a(self, collection: str | None) -> dict[str, object]:
        """Marker present: run the active flow (auto-register if needed, sync)."""
        if collection is None:
            registered_or_short_circuit = self._auto_register()
            if isinstance(registered_or_short_circuit, dict):
                return registered_or_short_circuit
            collection = registered_or_short_circuit
        return self._envelope(self._active_context(collection))

    def _path_b(self) -> dict[str, object]:
        """No marker, no coverage: nudge the operator to run ``quarry enable``.

        Read-only: the covering registration was already checked to reach
        here, but nothing is written — the daemon is not consulted, no
        registration is written, no guide is deposited. § 2.3 reserves both
        mutations to the explicit ``enable`` verb.
        """
        return self._envelope(SessionStartTemplates.nudge_enable(self._directory))

    def _path_c(self, collection: str) -> dict[str, object]:
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
        return self._envelope(
            SessionStartTemplates.drift_surface(self._directory, collection)
        )

    def _auto_register(self) -> str | dict[str, object]:
        """Register the covering row and return its collection name.

        Returns the collection ``str`` on success. Returns a response
        envelope (``dict``) when the flow must short-circuit — subsumption
        refusal or daemon-unreachable defer.
        """
        if self._conn.has_registrations_under(self._directory):
            logger.warning(
                "session-start: existing child registrations found under %s; "
                "skipping auto-register to prevent subsumption. Run "
                "'quarry enable %s' to explicitly register the parent.",
                self._directory,
                self._directory,
            )
            return self._envelope(SessionStartTemplates.subsumption(self._directory))
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
                return self._envelope(
                    SessionStartTemplates.daemon_unreachable(self._directory)
                )
            collection = self._resolver.unique_collection_name(
                self._directory, chunk_collections
            )
        self._conn.register_directory(self._directory, collection)
        logger.info(
            "session-start: auto-registered %s as '%s'",
            self._directory,
            collection,
        )
        return collection

    def _active_context(self, collection: str) -> str:
        """Build the active-mode ``additionalContext`` string."""
        captures_collection = f"{collection}-captures"
        sync_status = SyncLock().launch_background_sync()
        counts = self._session_coverage(collection, captures_collection)
        if counts is None:
            return SessionStartTemplates.active_unreachable_coverage(
                self._directory, collection, captures_collection, sync_status
            )
        return SessionStartTemplates.active(
            self._directory,
            collection,
            captures_collection,
            counts,
            sync_status,
        )

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

    @staticmethod
    def _session_coverage(
        collection: str, captures_collection: str
    ) -> CoverageCounts | None:
        """Fetch per-repo coverage counts from the daemon.

        Returns ``None`` whenever the client cannot obtain counts — daemon
        unreachable, HTTP error (including 401 not-authorized), malformed
        response, or client misconfiguration.  ``ClientConfigError`` is a
        ``QuarryError`` subclass, so a single ``except QuarryError`` catches
        every failure mode the client hierarchy names.

        Mirrors ``_daemon_chunk_collections`` at the boundary: client-specific
        exceptions become a single ``None`` signal, so the SessionStart template
        can fall back to a coverage-unavailable message without importing the
        client exception hierarchy.  ``None`` is the documented "unavailable"
        contract here — the caller distinguishes it from the empty-catalog case
        where the daemon answered with zeros.
        """
        del captures_collection  # daemon derives the sibling name server-side
        from quarry.client import (  # noqa: PLC0415
            ClientConfigError,
            QuarryError,
            TargetResolver,
        )

        try:
            resp = TargetResolver.connect().coverage(collection)
        except (ClientConfigError, QuarryError):
            return None
        return {
            "documents_indexed": resp.documents_indexed,
            "transcripts_captured": resp.transcripts_captured,
            "memories_saved": resp.memories_saved,
        }

    @staticmethod
    def _envelope(context: str) -> dict[str, object]:
        """Wrap *context* in the SessionStart hook-response envelope."""
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        }


def handle_post_web_fetch(payload: dict[str, object]) -> dict[str, object]:
    """Handle PostToolUse on WebFetch.

    Sends the already-fetched page to the daemon, which extracts, scrubs, and
    stores it in the project's ``<repo>-captures`` collection.  Uses the payload
    ``tool_response`` directly — no second fetch.  When the payload has no usable
    content, the daemon re-fetches through the SSRF-checked URL-ingest route
    instead.  The hook imports no engine — only the thin client and the
    lightweight URL/scrub helpers.  Looks up a PRIOR capture of this URL FIRST
    — after the (unconditional) send would always match — via
    :class:`~quarry.web_fetch_loop_closer.WebFetchLoopCloser`.
    """
    trace = HookTrace("post-web-fetch")
    cwd = HookPayload.as_dir(payload.get("cwd"))
    if cwd:
        on = load_hook_config(cwd).web_fetch
        trace.mark_config(on=on)
        if not on:
            trace.skip("config")
            return {}

    parsed = WebFetchPayload(payload)
    url = parsed.url
    if not url:
        trace.mark_payload(ok=False)
        trace.skip("no-url")
        return {}
    trace.mark_payload(ok=True)

    from quarry.api import CaptureIngestRequest, IngestRequest  # noqa: PLC0415
    from quarry.capture_url import CaptureUrl  # noqa: PLC0415
    from quarry.web_fetch_loop_closer import WebFetchLoopCloser  # noqa: PLC0415

    context = WebFetchLoopCloser(url, cwd).context()

    sender = DaemonCaptureSender()
    content = parsed.content
    if content:
        # Primary: hand the raw HTML to the daemon (it extracts, scrubs, chunks).
        # Carry the source URL so that if the HTML extracts to zero chunks (a
        # JS-rendered page) the daemon can re-fetch it server-side — the capture
        # route SSRF-gates source_url before the re-fetch — so the page is
        # captured, not silently dropped, and the client stays engine-free.
        sent = sender.send_capture(
            CaptureIngestRequest(
                content=content,
                cwd=cwd,
                document_name=CaptureUrl.for_web_fetch(url),
                format_hint="html",
                source_url=url,
            ),
            unreachable_log=_WEB_FETCH_UNREACHABLE,
        )
        detail = "inline"
    else:
        # Fallback: no usable content — the daemon re-fetches through the
        # SSRF-checked ingest route, scrubbing the page into <repo>-captures.
        logger.debug("post-web-fetch: no content in payload, re-fetching via daemon")
        sent = sender.send_ingest_url(
            IngestRequest(source=url, cwd=cwd, overwrite=True, scrub=True),
            unreachable_log=_WEB_FETCH_UNREACHABLE,
        )
        detail = "ingest-url"
    # The sender logs the specific failure class (misconfig, down, HTTP, malformed);
    # the trace only needs the binary distinction so the entered→ line closes on
    # every exit path (G6).
    if sent:
        trace.capture(detail)
    else:
        trace.error("daemon-unreachable")
    return context


# A web fetch writes NO durable local copy and backfill-sessions only re-ingests
# session transcripts, so a lost web capture is genuinely lost — the log must not
# promise a backfill that will never happen.
_WEB_FETCH_UNREACHABLE = (
    "web-fetch: daemon unreachable; page not indexed (re-fetch to retry)"
)


def _precompact_target(payload: dict[str, object]) -> tuple[str, str, Path] | None:
    """Return ``(cwd, session_id, resolved jsonl path)`` for a capturable compaction.

    ``None`` means the hook must no-op — the documented skip contract, not a
    failure: disabled by config, a missing ``transcript_path``/``session_id``, or
    a non-JSONL transcript suffix (defense-in-depth).  ``cwd`` may be empty (an
    unregistered directory still archives and ingests); the other two are
    required.
    """
    cwd = HookPayload.as_dir(payload.get("cwd"))
    if cwd and not load_hook_config(cwd).compaction:
        logger.debug("pre-compact: disabled by config")
        return None
    transcript_path = HookPayload.as_str(payload.get("transcript_path"))
    session_id = HookPayload.as_str(payload.get("session_id"))
    if not transcript_path or not session_id:
        logger.debug("pre-compact: missing transcript_path or session_id")
        return None
    resolved = HookPayload.resolve_jsonl(transcript_path, label="pre-compact")
    if resolved is None:
        return None
    return cwd, session_id, resolved


def handle_pre_compact(payload: dict[str, object]) -> dict[str, object]:
    """Handle PreCompact hook.

    Archives the raw transcript and writes the scrubbed ``.md`` capture locally,
    then sends the conversation text to the daemon to embed in the background.
    Returns the systemMessage immediately so compaction is never blocked, and a
    down daemon still leaves the durable local copies for ``backfill-sessions``.
    """
    trace = HookTrace("pre-compact")
    target = _precompact_target(payload)
    if target is None:
        trace.skip("payload")
        return {}
    cwd, session_id, tp = target
    trace.mark_payload(ok=True)

    outcome = SessionTranscriptCapture(
        cwd=cwd,
        session_id=session_id,
        transcript_path=tp,
        label="pre-compact",
        agent_handle=EthosConfig.agent_handle_at(cwd) if cwd else "",
    ).capture()

    if not outcome.text_captured:
        trace.skip("empty-transcript")
        return {}

    if not outcome.sent:
        trace.error("daemon-unreachable")
        return {
            "systemMessage": (
                "Warning: quarryd is not reachable, so this session was not "
                "indexed now. The raw JSONL archive and scrubbed capture are "
                "saved; run 'quarry backfill-sessions' to index them later."
            ),
        }

    trace.capture()
    return {
        "systemMessage": (
            "Capturing this session's conversation (background). "
            "Search with /find or show to retrieve it."
        ),
    }
