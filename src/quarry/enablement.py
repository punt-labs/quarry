"""Compose the repo-scoped CLAUDE.md enable/disable steps behind one object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.claude_import import ClaudeMdImport
from quarry.enabled_marker import EnabledMarker
from quarry.enablement_result import DisablementResult, EnablementResult
from quarry.file_lock import FileLock
from quarry.gitignore import QuarryGitignore
from quarry.guidance import REPO_IMPORT_LINE, Guidance

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["DisablementResult", "Enablement", "EnablementResult"]


@final
class Enablement:
    """Turn quarry's repo-scoped CLAUDE.md guidance composition on and off.

    Owns the ordering of the tool-enable-disable.md § 2.3 steps: ensure the
    repo's ``.gitignore`` excludes quarry's captures path, deposit the
    vendored guide, register the one bare ``@``-import line, and write the
    ``enabled`` marker — in that order, fail-closed: every later step (and
    the config write in ``enable_project()`` after it) only makes capture
    writing MORE live, so a mid-sequence failure never leaves the repo
    "enabled and unprotected." ``disable`` reverses the last three; the
    ``.gitignore`` line is additive-only — not ours to prune an ignore rule
    the user may keep for other reasons — and plays no part in the § 2.11
    invariant below.

    The enforced invariant (§ 2.11) is one-directional: marker present ⇒
    import present. Both operations make the near-infallible marker their
    commit point — ``enable`` registers the import before touching the
    marker, ``disable`` removes the marker before pruning the import — so a
    mid-operation failure can only leave import-present + marker-absent, the
    recoverable state a re-run reconciles, never marker-present +
    import-absent (a marker advertising guidance that is not wired in).

    A single :class:`FileLock` on the CLAUDE.md path wraps the whole
    register+marker (and marker+prune) sequence so the marker change and the
    import edit commit atomically w.r.t. a concurrent op — without it, a
    concurrent ``enable``/``disable`` could interleave and strand the
    marker. The lock is reentrant, so ``register``/``prune`` re-acquiring it
    for the edit itself share the one hold.
    """

    __slots__ = ("_gitignore", "_guidance", "_import", "_marker")

    _guidance: Guidance
    _marker: EnabledMarker
    _import: ClaudeMdImport
    _gitignore: QuarryGitignore

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._guidance = Guidance(root)
        self._marker = EnabledMarker(root)
        self._import = ClaudeMdImport(root / "CLAUDE.md")
        self._gitignore = QuarryGitignore(root)
        return self

    def enable(self) -> EnablementResult:
        """Ensure the ignore, deposit the guide, register the import, write the marker.

        The ``.gitignore`` ensure runs FIRST — protection lands before any
        step that depends on it, so a re-enable still backfills a missing
        exclusion on a repo enabled before this step existed. Register,
        marker, and the ensure each report whether they changed anything, so
        an idempotent re-enable returns all three booleans ``False``.
        Register runs before the near-infallible marker write so a register
        failure leaves neither present, never the marker-without-import
        state § 2.11 forbids. Register and marker share one
        :class:`FileLock` so a concurrent ``disable`` cannot strand the
        marker without its import.
        """
        gitignore_ensured = self._gitignore.ensure()
        self._guidance.deposit()
        with FileLock(self._import.path):
            import_registered = self._import.register(REPO_IMPORT_LINE)
            enabled_marker_written = self._marker.write()
        return EnablementResult(
            guide_deposited=True,
            enabled_marker_written=enabled_marker_written,
            import_registered=import_registered,
            gitignore_ensured=gitignore_ensured,
        )

    def disable(self) -> DisablementResult:
        """Remove the marker and prune the import atomically; leave the guide dormant.

        Guarantees the § 2.11 invariant marker-present ⇒ import-present: the
        marker + import teardown commits atomically under one
        :class:`FileLock`, and the near-infallible marker removal runs before
        the fallible prune so a mid-call failure only ever leaves the
        recoverable marker-absent + import-present state, never
        marker-present + import-absent. A concurrent ``enable`` cannot
        interleave under the shared lock.

        Deregistering the sync collection is the CALLER's responsibility.
        This method owns marker + import atomicity only; it deliberately
        depends on no daemon/HTTP client so the file-system commit point
        stays pure stdlib. The orchestrator (:func:`quarry.enable.disable_project`)
        sequences the deregister AFTER this call, so a deregister failure
        leaves a coherent disabled surface (marker-absent, import-absent)
        with only a runtime registration residue a retry converges.

        A :class:`~quarry.safe_paths.SafeRepoPath` refusal (a hostile symlinked
        ancestor) is caught so it cannot abort before the prune and strand the
        ``@``-import a prior teardown already acted on. The refused marker is
        not a real in-repo marker (``is_present()`` is ``False``), so treating it
        as absent and pruning anyway keeps the recoverable invariant. A genuine
        unlink error still propagates, leaving the recoverable marker-present +
        import-present enabled state, never marker-present + import-absent.
        """
        with FileLock(self._import.path):
            try:
                enabled_marker_removed = self._marker.remove()
            except ValueError:
                enabled_marker_removed = False
            import_pruned = self._import.prune(REPO_IMPORT_LINE)
        return DisablementResult(
            import_pruned=import_pruned,
            enabled_marker_removed=enabled_marker_removed,
        )
