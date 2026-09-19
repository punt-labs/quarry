"""Bootstrap per-identity quarry memory in the global and vendored ethos trees."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, final

from quarry.doctor_ethos import EthosExtDiagnostics
from quarry.ethos_ext_scan import ExtScanOutcome
from quarry.ethos_tree import EthosTree

__all__ = ["EthosMemoryBootstrap", "EthosMemoryResult"]

_GLOBAL_IDENTITIES = Path.home() / ".punt-labs" / "ethos" / "identities"


@dataclass(frozen=True, slots=True)
class EthosMemoryResult:
    """Outcome of an ethos-memory bootstrap, per identity handle.

    ``skipped`` is True when the global identities directory is absent (ethos
    not installed). ``vendored_updated`` names the identities whose committed
    ext file was refreshed — a working-tree diff the operator commits via PR.
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    already_set: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: bool = False
    vendored_updated: list[str] = field(default_factory=list)

    @property
    def memory_collections(self) -> list[str]:
        """Return the memory-collection name created for each new handle."""
        return [f"memory-{handle}" for handle in self.created]


@final
class EthosMemoryBootstrap:
    """Create each identity's ``quarry.yaml`` ext and keep its memory guide current.

    The global tree is where ext files are *created*; a repo's vendored tree
    is refresh-only, because its roster is curated by ``ethos vendor`` and a
    file quarry invented there would be an uncommitted surprise. A handle
    lands in ``failed`` when its guide write raised an I/O or YAML error —
    the useful part never landed, so the caller must not report unqualified
    success for it.
    """

    __slots__ = ("_identities", "_vendored")

    _identities: Path
    # ``None`` is the documented "this repo has no vendored tree" contract.
    _vendored: Path | None

    def __new__(
        cls, identities: Path | None = None, vendored: Path | None = None
    ) -> Self:
        # None means "use the configured global identities dir" — read at call
        # time (not bound as a default) so a test can patch the module global.
        self = super().__new__(cls)
        self._identities = identities if identities is not None else _GLOBAL_IDENTITIES
        self._vendored = vendored
        return self

    @classmethod
    def for_repo(cls, directory: Path) -> Self:
        """Return a bootstrap that also refreshes the vendored tree above *directory*.

        ``None`` from the locator means the repo has no vendored tree, and the
        bootstrap then refreshes the global tree alone.
        """
        return cls(vendored=EthosTree(directory).vendored_identities())

    def run(self) -> EthosMemoryResult:
        """Bootstrap the global tree and refresh both; return the per-handle outcome.

        The vendored refresh does not depend on a global install: under
        ``repo-only`` resolution the vendored ext is the one an identity
        actually receives, so it is kept current even where ethos has never
        been installed for the operator.
        """
        # Refresh-only: the vendored roster is curated by ``ethos vendor``, so an
        # ext file quarry invented there would be an uncommitted surprise.
        vendored_scan = (
            EthosExtDiagnostics.refresh_vendored(self._vendored)
            if self._vendored is not None
            else ExtScanOutcome()
        )
        if not self._identities.is_dir():
            return EthosMemoryResult(
                failed=list(vendored_scan.failed_handles),
                skipped=True,
                vendored_updated=list(vendored_scan.updated),
            )
        created = [
            identity.stem
            for identity in sorted(self._identities.glob("*.yaml"))
            if self._ensure_ext(identity.stem)
        ]
        global_scan = EthosExtDiagnostics().refresh(self._identities)
        return EthosMemoryResult(
            created=created,
            updated=list(global_scan.updated),
            already_set=list(global_scan.already_set),
            failed=[*global_scan.failed_handles, *vendored_scan.failed_handles],
            skipped=False,
            vendored_updated=list(vendored_scan.updated),
        )

    def _ensure_ext(self, handle: str) -> bool:
        """Create the identity's ``quarry.yaml`` ext if absent; report creation."""
        ext_dir = self._identities / f"{handle}.ext"
        ext_dir.mkdir(exist_ok=True)
        quarry_yaml = ext_dir / "quarry.yaml"
        if quarry_yaml.exists():
            return False
        quarry_yaml.write_text(
            f"memory_collection: memory-{handle}\n", encoding="utf-8"
        )
        return True
