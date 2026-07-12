"""Facade orchestrating the commit-time re-scrub gate and push for one project.

``CaptureSync.run`` enforces the security-critical ordering: stage -> re-scrub
the staged captures -> stage again -> I/O-race guard (abort BEFORE commit on any
residual) -> commit -> visibility gate -> push.  ``push`` lives on the normal
path only — never in a ``finally`` — because a ``finally`` would push even after
an abort, and ``git push`` ships every unpushed commit, so a single poisoned
commit is a permanent leak.  Operation is fail-open (a push/network/git failure
never blocks a session); the gate is fail-closed (a re-scrub/verify/git
exception aborts the commit, never falling through to push).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.shadow.config import ShadowConfig
from quarry.shadow.repo import ShadowRepo, Visibility
from quarry.shadow.rescrub import CaptureReScrubber

if TYPE_CHECKING:
    from quarry.config import Settings

logger = logging.getLogger(__name__)

_CAPTURES_SUBPATH = (".punt-labs", "quarry", "captures")


@dataclass(frozen=True, slots=True)
class ShadowSyncResult:
    """Outcome of one shadow sync run."""

    pushed: bool
    committed: bool
    rescrubbed: int
    aborted_reason: str
    race_failures: tuple[Path, ...]

    @classmethod
    def aborted(
        cls,
        reason: str,
        *,
        rescrubbed: int = 0,
        committed: bool = False,
        races: tuple[Path, ...] = (),
    ) -> Self:
        """Build a result for a run that did not push."""
        return cls(
            pushed=False,
            committed=committed,
            rescrubbed=rescrubbed,
            aborted_reason=reason,
            race_failures=races,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable dict with identical keys on every surface."""
        return {
            "pushed": self.pushed,
            "committed": self.committed,
            "rescrubbed": self.rescrubbed,
            "aborted_reason": self.aborted_reason,
            "race_failures": [str(p) for p in self.race_failures],
        }


@final
class CaptureSync:
    """Compose config, repo, and re-scrubber into the commit-time push flow."""

    __slots__ = ("_config", "_directory", "_repo", "_rescrubber")

    _config: ShadowConfig
    _directory: Path
    _repo: ShadowRepo
    _rescrubber: CaptureReScrubber

    def __new__(
        cls,
        directory: Path,
        config: ShadowConfig,
        repo: ShadowRepo,
        rescrubber: CaptureReScrubber,
    ) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._config = config
        self._repo = repo
        self._rescrubber = rescrubber
        return self

    @classmethod
    def from_directory(cls, directory: Path) -> Self | None:
        """Build a sync for *directory*, or None when no ``shadow:`` block exists.

        None is the documented "not configured" contract; the automatic sync
        path skips a directory whose config has no shadow block.
        """
        config = ShadowConfig.from_project(directory)
        if config is None:
            return None
        captures_dir = directory.joinpath(*_CAPTURES_SUBPATH)
        repo = ShadowRepo(captures_dir, directory, config.remote)
        rescrubber = CaptureReScrubber(captures_dir)
        return cls(directory, config, repo, rescrubber)

    @classmethod
    def push_registered(
        cls, settings: Settings, *, fail_open: bool
    ) -> dict[str, ShadowSyncResult]:
        """Run the shadow sync over every enabled registered directory."""
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        results: dict[str, ShadowSyncResult] = {}
        conn = SyncRegistry(settings.registry_path)
        try:
            for reg in conn.list_registrations():
                sync = cls.from_directory(Path(reg.directory))
                if sync is not None and sync.enabled:
                    results[reg.collection] = sync.run(fail_open=fail_open)
        finally:
            conn.close()
        return results

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def bootstrap(self, *, create: bool = False) -> bool:
        """Prepare the nested repo without pushing (``quarry captures init``).

        When *create* is set, first create the private remote via ``gh`` and
        verify it is private before touching the working tree.
        """
        if create and not self._repo.create_remote():
            return False
        return self._repo.bootstrap()

    def run(self, *, fail_open: bool = True) -> ShadowSyncResult:
        """Execute the commit-time gate and push. Fail-open returns a result."""
        try:
            return self._execute()
        except Exception:
            logger.exception("shadow: capture sync failed for %s", self._directory)
            if not fail_open:
                raise
            return ShadowSyncResult.aborted("exception")

    def _execute(self) -> ShadowSyncResult:
        if not self._config.enabled:
            return ShadowSyncResult.aborted("disabled")
        if not self._repo.bootstrap():
            return ShadowSyncResult.aborted("bootstrap-refused")

        # None in the first slot means "index prepared, proceed to commit".
        abort, rescrubbed = self._stage_and_verify()
        if abort is not None:
            return abort

        committed = self._repo.commit()
        gate = self._visibility_gate()
        if gate is not None:
            return ShadowSyncResult.aborted(
                gate, rescrubbed=rescrubbed, committed=committed
            )
        return ShadowSyncResult(
            pushed=self._push_and_warn(),
            committed=committed,
            rescrubbed=rescrubbed,
            aborted_reason="",
            race_failures=(),
        )

    def _push_and_warn(self) -> bool:
        """Push and, on failure, log fail-open — captures stay off the remote.

        Returns whether the push succeeded.  A ``False`` is not an error (the
        commit is safe locally) but IS leak-relevant, so it is logged rather
        than swallowed silently.
        """
        pushed = self._repo.push()
        if not pushed:
            logger.warning(
                "shadow: committed but push failed for %s (offline?); "
                "captures remain unpushed",
                self._directory,
            )
        return pushed

    def _stage_and_verify(self) -> tuple[ShadowSyncResult | None, int]:
        """Stage, re-scrub, re-stage, and verify the STAGED blobs.

        Return ``(abort_result, rescrubbed)``.  ``abort_result`` is non-None
        when the run must abort before committing; ``None`` signals the index is
        clean and the caller may commit.  Verifying the staged blobs (what the
        commit ships) rather than the working tree closes the gap where a silent
        re-stage failure leaves the index unscrubbed while the disk reads clean.
        """
        if not self._repo.stage():
            return ShadowSyncResult.aborted("stage-failed"), 0
        rescrubbed = self._rescrubber.rescrub_all()
        if not self._repo.stage():
            logger.warning("shadow: re-stage after re-scrub failed; aborting commit")
            return ShadowSyncResult.aborted("stage-failed", rescrubbed=rescrubbed), 0
        return self._verify_staged(rescrubbed), rescrubbed

    def _verify_staged(self, rescrubbed: int) -> ShadowSyncResult | None:
        """Return a race-guard abort if any STAGED blob is unscrubbed, else None.

        None signals a clean index — the caller may commit.
        """
        races = self._rescrubber.verify_staged_clean(self._repo.staged_captures())
        if not races:
            return None
        logger.warning(
            "shadow: aborting commit; %d staged file(s) failed the race guard",
            len(races),
        )
        return ShadowSyncResult.aborted(
            "race-guard", rescrubbed=rescrubbed, races=tuple(races)
        )

    def _visibility_gate(self) -> str | None:
        """Return an abort reason if the remote is unsafe, else None to proceed.

        None is the "gate passed" state, not a failure signal — the remote is
        verified private, or unknown-but-acknowledged.
        """
        visibility = self._repo.remote_visibility()
        if visibility is Visibility.PUBLIC:
            logger.warning("shadow: remote is PUBLIC; refusing to push captures")
            return "public-remote"
        if visibility is Visibility.UNKNOWN and not self._config.acknowledge_unverified:
            logger.warning(
                "shadow: cannot verify the remote is private; set "
                "shadow.acknowledge_unverified or pass --force",
            )
            return "unverified-visibility"
        return None
