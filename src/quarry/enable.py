"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, final

from quarry.claude_permissions import PermissionChange, ProjectPermissions
from quarry.ethos_memory import EthosMemoryBootstrap, EthosMemoryResult
from quarry.home_paths import QuarryHome
from quarry.local_disable import ConfigDisposal, GuardedDisable
from quarry.project_config import ProjectConfig

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
    from quarry.skills_install import SkillOutcome

logger = logging.getLogger(__name__)


class RegistryClient(Protocol):
    """The daemon-registry surface enable/disable need — the client is the adapter.

    Depending on this port (not the concrete ``QuarryClient``) keeps enable/disable
    off the client package's import graph and lets a test supply an in-memory
    stand-in.
    """

    def list_registrations(self) -> RegistrationList:
        """Read the daemon's active directory registrations and retained markers."""
        ...

    def register(self, req: RegisterRequest) -> TaskAccepted:
        """Queue a directory registration and return its accepted task identifier."""
        ...

    def deregister(self, req: DeregisterRequest) -> DeregisterAccepted:
        """Queue removal of a directory registration under its keep-data policy."""
        ...

    def delete_collection(self, req: DeleteCollectionRequest) -> TaskAccepted:
        """Queue deletion of a collection's stored content."""
        ...


@dataclass(frozen=True, slots=True)
class EnableResult:
    """Result of enabling quarry for a project directory.

    The four CLAUDE.md/.gitignore fields track the § 2.3 steps: guide
    deposit, ``enabled`` marker, ``@``-import line, and ``.gitignore`` entry.
    ``ethos`` is the bootstrap's own per-handle outcome, carried whole rather
    than flattened into parallel lists. ``permissions`` is the § 2.8 allow-rule
    grant in the repo's ``.claude/settings.json``. ``skills_deposited``
    is the cross-harness skill-deposit outcome, passed through from
    ``Enablement.enable()`` unchanged.  ``covered_by`` is the parent
    registration reused instead of registering this directory, else empty.
    """

    directory: str
    collection: str
    captures_collection: str
    config_path: str = ""
    created_registration: bool = False
    covered_by: str = ""
    guide_deposited: bool = False
    enabled_marker_written: bool = False
    import_registered: bool = False
    gitignore_ensured: bool = False
    ethos: EthosMemoryResult = field(default_factory=EthosMemoryResult)
    permissions: PermissionChange = field(default_factory=PermissionChange)
    skills_deposited: tuple[SkillOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class DisableResult:
    """Describe registry removal and the asynchronous purge after disabling Quarry.

    Result of disabling quarry.  ``removed`` is the registry file count the
    daemon reported synchronously; the chunk purge runs as a background task.

    ``disable`` is non-destructive of the vendored guide (§ 2.9): it prunes the
    ``@``-import line and deletes the ``enabled`` marker, leaving the deposited
    ``.punt-labs/quarry/CLAUDE.md`` dormant on disk.

    ``covered_by`` is the parent registration left in place when the directory
    was only covered by it (nothing of the parent's is deregistered or purged).
    ``capturing_marker`` is the directory whose ``enabled`` marker still keeps
    capture live under the child once its own marker is gone (empty when none
    does); it is found by the session-start hook's own search.  ``config_kept`` is
    whether the child's ``config.md`` was left in place: it changes what the
    hooks do, so removing it would discard the user's opt-outs.
    ``config_why`` refines a kept config: ``"unreadable"`` (the hooks cannot read
    it and apply nothing from it) or ``"linked"`` (it is behind a symlink).
    """

    directory: str
    collection: str
    captures_collection: str
    removed: int = 0
    config_removed: bool = False
    import_pruned: bool = False
    enabled_marker_removed: bool = False
    permissions: PermissionChange = field(default_factory=PermissionChange)
    covered_by: str = ""
    capturing_marker: str = ""
    config_kept: bool = False
    config_why: str = ""


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
    from quarry.registrar import Registrar  # noqa: PLC0415

    # expanduser BEFORE resolve: a bare "~/proj" otherwise resolves against cwd
    # ("./~/proj"), targeting the wrong directory.
    directory = QuarryHome.expanded(directory).resolve()
    if not directory.is_dir():
        msg = f"directory not found: {directory}"
        raise ValueError(msg)

    resolution = Registrar(client).resolve(directory, collection_override)

    # Global ext files are created and refreshed; the repo's vendored tree (a
    # repo-only identity's actual source layer) is refreshed, never created.
    ethos = EthosMemoryBootstrap.for_repo(directory).run()

    # Enablement runs BEFORE the config write: config.md's compaction flag
    # has no gitignore/marker dependency, so it makes hook-triggered capture
    # writes live the moment it lands. This order keeps a mid-failure repo
    # "not yet enabled" rather than "enabled and unprotected".
    claudemd = Enablement(directory).enable()
    config_path = ProjectConfig().write(directory)
    return EnableResult(
        directory=str(directory),
        collection=resolution.collection,
        captures_collection=f"{resolution.collection}-captures",
        config_path=config_path,
        created_registration=resolution.created,
        covered_by=resolution.covered_by,
        guide_deposited=claudemd.guide_deposited,
        enabled_marker_written=claudemd.enabled_marker_written,
        import_registered=claudemd.import_registered,
        gitignore_ensured=claudemd.gitignore_ensured,
        ethos=ethos,
        # Running enable is the user's consent to quarry's allow rules. They
        # are granted last, once every other step has succeeded, so a failure
        # earlier never leaves allow rules in a half-enabled project.
        permissions=ProjectPermissions(directory).grant(),
        skills_deposited=claudemd.skills_deposited,
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
    forbidden marker-present-without-a-collection state if disabling raises.
    """
    directory = QuarryHome.expanded(directory).resolve()
    return _DisableOrchestrator(directory, client, keep_data=keep_data).run()


@final
class _DisableOrchestrator:
    """Sequence the § 2.3 disable steps in the § 2.11 commit-point order.

    Instances are single-use. Owning the (directory, client, covering,
    keep_data, parent) state as slots lets each fallible step be a named method the
    caller can drive in an explicit order — the same order § 5.4's regression
    test asserts against — and keeps the top-level ``disable_project``
    function a thin driver over one live class.
    """

    __slots__ = ("_client", "_covering", "_directory", "_keep_data", "_parent")

    _directory: Path
    _client: RegistryClient
    _covering: RegistrationInfo | None
    _keep_data: bool
    _parent: str

    def __new__(
        cls,
        directory: Path,
        client: RegistryClient,
        *,
        keep_data: bool,
    ) -> Self:
        from quarry.registrations import Registrations  # noqa: PLC0415

        covering = Registrations.from_list(client.list_registrations()).covering(
            directory
        )
        # A CHILD of a registered parent owns no registration: disable undoes
        # its own files but never deregisters or purges the parent's collection.
        parent = ""
        if covering is not None and covering.directory != str(directory):
            parent, covering = covering.directory, None
        self = super().__new__(cls)
        self._parent = parent
        self._directory = directory
        self._client = client
        self._covering = covering
        self._keep_data = keep_data
        return self

    def run(self) -> DisableResult:
        """Drive the ordered steps and assemble the result."""
        config = self._settle_config()
        claudemd, capturing_marker = self._disable_enablement()
        permissions = ProjectPermissions(self._directory).revoke()
        removed = self._deregister_covering()
        self._purge_captures()
        collection = self._covering.collection if self._covering is not None else ""
        return DisableResult(
            directory=str(self._directory),
            collection=collection,
            captures_collection=f"{collection}-captures" if collection else "",
            removed=removed,
            config_removed=config.removed,
            config_kept=config.kept,
            config_why=config.why,
            import_pruned=claudemd.import_pruned,
            enabled_marker_removed=claudemd.enabled_marker_removed,
            permissions=permissions,
            covered_by=self._parent,
            capturing_marker=capturing_marker,
        )

    def _settle_config(self) -> ConfigDisposal:
        """Remove ``config.md``, or keep it when the covering parent still reads it."""
        return ConfigDisposal.settle(self._directory, covered=bool(self._parent))

    def _disable_enablement(self) -> tuple[DisablementResult, str]:
        """Remove the marker and prune the ``@``-import under one FileLock.

        The § 2.11 commit point: marker-absent + import-present is the only
        recoverable failure state; ``Enablement.disable`` guarantees the
        biconditional.

        Also returns the directory whose marker still captures the child, or
        empty. The search is the session-start hook's own, run after this
        directory's marker is gone, so the report cannot disagree with the hook.
        """
        guard = GuardedDisable(self._directory)
        return guard.run(), guard.capturing_marker(self._parent)

    def _deregister_covering(self) -> int:
        """Deregister the covering collection via the daemon; return removed row count.

        Runs AFTER the § 2.11 commit point so a deregister failure leaves a
        coherent disabled surface — never marker-present + import-present
        with no functional collection behind them.
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
