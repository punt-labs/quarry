"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, final

from quarry.safe_paths import SafeRepoPath

if TYPE_CHECKING:
    from quarry.api import (
        DeleteCollectionRequest,
        DeregisterAccepted,
        DeregisterRequest,
        RegisterRequest,
        RegistrationInfo,
        RegistrationList,
        TaskAccepted,
    )
    from quarry.enablement_result import DisablementResult
    from quarry.registrations import Registrations

logger = logging.getLogger(__name__)


class RegistryClient(Protocol):
    """The daemon-registry surface enable/disable need — the client is the adapter.

    Depending on this port (not the concrete ``QuarryClient``) keeps enable/disable
    off the client package's import graph and lets a test supply an in-memory
    stand-in.
    """

    def list_registrations(self) -> RegistrationList: ...
    def register(self, req: RegisterRequest) -> TaskAccepted: ...
    def deregister(self, req: DeregisterRequest) -> DeregisterAccepted: ...
    def delete_collection(self, req: DeleteCollectionRequest) -> TaskAccepted: ...


@dataclass(frozen=True, slots=True)
class EnableResult:
    """Result of enabling quarry for a project directory.

    The four CLAUDE.md/.gitignore fields track the § 2.3 steps: guide
    deposit, ``enabled`` marker, ``@``-import line, and ``.gitignore`` entry.
    """

    directory: str
    collection: str
    captures_collection: str
    memory_collections: list[str] = field(default_factory=list)
    config_path: str = ""
    created_registration: bool = False
    guide_deposited: bool = False
    enabled_marker_written: bool = False
    import_registered: bool = False
    gitignore_ensured: bool = False
    ethos_skipped: bool = False
    ethos_updated: list[str] = field(default_factory=list)
    ethos_already_set: list[str] = field(default_factory=list)
    ethos_created: list[str] = field(default_factory=list)
    ethos_failed: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DisableResult:
    """Result of disabling quarry.  ``removed`` is the registry file count the
    daemon reported synchronously; the chunk purge runs as a background task.

    ``disable`` is non-destructive of the vendored guide (§ 2.9): it prunes the
    ``@``-import line and deletes the ``enabled`` marker, leaving the deposited
    ``.punt-labs/quarry/CLAUDE.md`` dormant on disk.
    """

    directory: str
    collection: str
    captures_collection: str
    removed: int = 0
    config_removed: bool = False
    import_pruned: bool = False
    enabled_marker_removed: bool = False


_CONFIG_TEMPLATE = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
  session_end: true
  web_search: true
  read: false
  subagent_stop: true
# shadow:                          # push redacted captures to a PRIVATE shadow repo
#   enabled: false                 # opt-in network+security action, off by default
#   remote: ""                     # empty -> derive <origin>-quarry from origin
#   acknowledge_unverified: false  # push even when gh cannot confirm private
---

# Quarry Project Configuration

Controls quarry's passive knowledge capture. Set any field to `false` to disable
that capture type; uncomment `shadow` to move redacted captures off the public
repo into a per-project private shadow (`<repo>` -> `<repo>-quarry`).

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
- `session_end`: capture session transcripts on session close (fires even when
  a session never compacts)
- `web_search`: auto-ingest a digest of `WebSearch` results
- `read`: capture prose files (.md/.pdf/.docx/...) read from outside the tree —
  default `false` because `Read` fires often and has the highest secret-leak
  surface; opt in after confirming the filter set is clean
- `subagent_stop`: capture subagent transcripts on completion
- `shadow`: pre-create the private repo, then set `enabled: true`
"""


def enable_project(
    directory: Path,
    client: RegistryClient,
    collection_override: str = "",
) -> EnableResult:
    """Enable quarry knowledge capture for a project directory.

    The registry is the daemon's (DES-031 I2): coverage is computed from its
    ``RegistrationList`` and a new registration is dispatched via ``client``, never
    a local ``SyncRegistry``.  The project files (config.md, CLAUDE.md, ethos ext)
    are the client's and are written locally.
    """
    from quarry.enablement import Enablement  # noqa: PLC0415
    from quarry.ethos_memory import EthosMemoryBootstrap  # noqa: PLC0415
    from quarry.registrar import Registrar  # noqa: PLC0415

    # expanduser BEFORE resolve: a bare "~/proj" otherwise resolves against cwd
    # ("./~/proj"), targeting the wrong directory.
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        msg = f"directory not found: {directory}"
        raise ValueError(msg)

    collection, created = Registrar(client).resolve(directory, collection_override)

    captures_collection = f"{collection}-captures"

    ethos = EthosMemoryBootstrap().run()

    # Enablement runs BEFORE the config write: config.md's compaction flag
    # has no gitignore/marker dependency, so it makes hook-triggered capture
    # writes live the moment it lands. This order keeps a mid-failure repo
    # "not yet enabled" rather than "enabled and unprotected".
    claudemd = Enablement(directory).enable()
    if claudemd.import_registered:
        logger.info("Registered quarry @-import in CLAUDE.md")
    config_path = _write_project_config(directory)

    return EnableResult(
        directory=str(directory),
        collection=collection,
        captures_collection=captures_collection,
        memory_collections=ethos.memory_collections,
        config_path=config_path,
        created_registration=created,
        guide_deposited=claudemd.guide_deposited,
        enabled_marker_written=claudemd.enabled_marker_written,
        import_registered=claudemd.import_registered,
        gitignore_ensured=claudemd.gitignore_ensured,
        ethos_skipped=ethos.skipped,
        ethos_updated=ethos.updated,
        ethos_already_set=ethos.already_set,
        ethos_created=ethos.created,
        ethos_failed=ethos.failed,
    )


def disable_project(
    directory: Path,
    client: RegistryClient,
    *,
    keep_data: bool = False,
) -> DisableResult:
    """Disable quarry knowledge capture for a project directory.

    Idempotent and retry-safe. Resolves the covering registration, removes the
    local capture config, teardown-commits the marker + ``@``-import atomically
    via :class:`~quarry.enablement.Enablement`, deregisters the sync collection
    via the daemon, then purges the ``-captures`` sibling best-effort (unless
    ``keep_data``). The registry is never mutated through a local
    ``SyncRegistry``.

    A directory with no covering registration is NOT an error: it was never
    enabled, or a prior partial disable already removed it. The local project
    files are still cleaned and the call succeeds, so a retry after a
    mid-teardown failure always converges to fully-disabled. The deregister
    runs AFTER ``Enablement.disable`` so a deregister failure leaves a
    coherent disabled surface (marker-absent, import-absent) with only a
    runtime registration residue a retry converges — never the § 2.11
    forbidden marker-present-without-a-collection state that the old order
    could produce if ``Enablement.disable`` raised.
    """
    from quarry.registrations import Registrations  # noqa: PLC0415

    directory = directory.expanduser().resolve()
    view = Registrations.from_list(client.list_registrations())
    return _DisableOrchestrator(directory, client, view, keep_data=keep_data).run()


@final
class _DisableOrchestrator:
    """Sequence the § 2.3 disable steps in the § 2.11 commit-point order.

    Instances are single-use. Owning the (directory, client, covering,
    keep_data) state as slots lets each fallible step be a named method the
    caller can drive in an explicit order — the same order § 5.4's regression
    test asserts against — and keeps the top-level ``disable_project``
    function a thin driver over one live class.
    """

    __slots__ = ("_client", "_covering", "_directory", "_keep_data")

    _directory: Path
    _client: RegistryClient
    _covering: RegistrationInfo | None
    _keep_data: bool

    def __new__(
        cls,
        directory: Path,
        client: RegistryClient,
        view: Registrations,
        *,
        keep_data: bool,
    ) -> Self:
        covering = view.covering(directory)
        # A CHILD of a registered parent is a real error — the parent covers it;
        # never silently deregister the parent. This guard stays fatal.
        if covering is not None and covering.directory != str(directory):
            msg = (
                f"no registration for {directory}; "
                f"it is covered by parent registration at {covering.directory}"
            )
            raise ValueError(msg)
        self = super().__new__(cls)
        self._directory = directory
        self._client = client
        self._covering = covering
        self._keep_data = keep_data
        return self

    def run(self) -> DisableResult:
        """Drive the ordered steps and assemble the result."""
        config_removed = self._remove_config_file()
        claudemd = self._disable_enablement()
        removed = self._deregister_covering()
        self._purge_captures()
        collection = self._covering.collection if self._covering is not None else ""
        return DisableResult(
            directory=str(self._directory),
            collection=collection,
            captures_collection=f"{collection}-captures" if collection else "",
            removed=removed,
            config_removed=config_removed,
            import_pruned=claudemd.import_pruned,
            enabled_marker_removed=claudemd.enabled_marker_removed,
        )

    def _remove_config_file(self) -> bool:
        """Delete ``config.md`` via :class:`SafeRepoPath`; return whether removed.

        A symlinked ``.punt-labs`` ancestor makes the unlink refuse; catching
        that so it cannot abort before ``Enablement.disable`` prunes the
        ``@``-import a prior teardown already acted on. A refused config is
        not a real in-repo file, so treating it as absent and continuing is
        correct.
        """
        try:
            return SafeRepoPath(
                self._directory, (".punt-labs", "quarry", "config.md")
            ).remove()
        except ValueError:
            return False

    def _disable_enablement(self) -> DisablementResult:
        """Remove the marker and prune the ``@``-import under one FileLock.

        The § 2.11 commit point: marker-absent + import-present is the only
        recoverable failure state; ``Enablement.disable`` guarantees the
        biconditional.
        """
        from quarry.enablement import Enablement  # noqa: PLC0415

        claudemd = Enablement(self._directory).disable()
        if claudemd.import_pruned:
            logger.info("Removed quarry @-import from CLAUDE.md")
        return claudemd

    def _deregister_covering(self) -> int:
        """Deregister the covering collection via the daemon; return removed row count.

        Runs AFTER the § 2.11 commit point so a deregister failure leaves a
        coherent disabled surface — never marker-present + import-present
        with no functional collection behind them (the old-order defect).
        """
        if self._covering is None:
            return 0
        from quarry.api import DeregisterRequest  # noqa: PLC0415

        collection = self._covering.collection
        return self._client.deregister(
            DeregisterRequest(collection=collection, keep_data=self._keep_data)
        ).removed

    def _purge_captures(self) -> None:
        """Best-effort delete the ``-captures`` sibling; log and swallow rejections.

        Once the registration is gone a retry cannot re-derive the captures
        name, so this is the one attempt. A rejection here does not fail the
        whole command — the primary teardown already succeeded.
        """
        if self._covering is None or self._keep_data:
            return
        from quarry.api import DeleteCollectionRequest  # noqa: PLC0415
        from quarry.client.errors import QuarryError  # noqa: PLC0415

        captures = f"{self._covering.collection}-captures"
        try:
            self._client.delete_collection(DeleteCollectionRequest(name=captures))
        except QuarryError:
            logger.warning(
                "captures purge for %s was rejected; its chunks may remain, but "
                "the project is fully disabled (deregistered + local files removed)",
                captures,
            )


def _write_project_config(directory: Path) -> str:
    """Write config.md exclusively (no overwrite, no symlink follow); return its path.

    Routes through :class:`quarry.safe_paths.SafeRepoPath` so a hostile repo
    cannot redirect the create outside the repo via a symlinked ``.punt-labs``
    ancestor or a symlinked ``config.md`` leaf. An existing regular config is
    left untouched (idempotent); a non-regular entry at the path is refused.
    """
    config = SafeRepoPath(directory, (".punt-labs", "quarry", "config.md"))
    config.create_exclusive(_CONFIG_TEMPLATE, mode=0o644)
    return str(config.path)
