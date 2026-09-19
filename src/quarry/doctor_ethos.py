"""Refresh the quarry memory guide in each ethos identity's ``quarry.yaml`` ext."""

from __future__ import annotations

from pathlib import Path
from typing import final

import yaml

from quarry.atomic_file import AtomicFile
from quarry.ethos_ext_block import SessionContextBlock
from quarry.ethos_ext_scan import ExtFailure, ExtScanOutcome, ExtWriteResult
from quarry.ethos_tree import EthosTree
from quarry.path_guard import FOLLOW_SYMLINKS, PathGuard, SealedTree
from quarry.results import CheckResult


@final
class EthosExtDiagnostics:
    """Write the current memory guide into each identity's ``quarry.yaml`` ext.

    Idempotent: a file already carrying the current guide, or a hand-authored
    ``session_context``, is left unchanged; a stale quarry guide is spliced up
    to the current version in place. Identity directories without a
    ``quarry.yaml`` (quarry not configured for that identity) are skipped and
    never created here.
    """

    __slots__ = ()

    @staticmethod
    def configure(identities_dir: Path | None = None) -> CheckResult:
        """Best-effort install step: refresh the guide across the global tree.

        ``quarry install`` has no repo context, so this touches the global
        identities only; ``quarry enable`` handles a repo's vendored tree.
        """
        if identities_dir is None:
            identities_dir = EthosTree.global_identities()
        name = "Ethos ext session_context"
        if not identities_dir.is_dir():
            return CheckResult(
                name=name,
                passed=True,
                message="ethos not installed, skipping",
                required=False,
            )
        outcome = EthosExtDiagnostics.refresh(identities_dir)
        if outcome.is_empty:
            return CheckResult(
                name=name,
                passed=True,
                message="no identities with quarry configured",
                required=False,
            )
        return CheckResult(
            name=name,
            passed=not outcome.failed,
            message=outcome.message(),
            required=False,
        )

    @staticmethod
    def refresh_vendored(identities_dir: Path) -> ExtScanOutcome:
        """Refresh a checkout's vendored ``identities/``, sealed at the checkout root.

        The vendored tree is cloned content: a committed symlink under it could
        redirect the guide write onto a file outside the repo (the operator's
        global ext, with an attacker-chosen handle in the guide). Sealing at
        the checkout root refuses any symlink component below it; the root
        itself is the operator's choice and may be one.
        """
        seal = SealedTree(EthosTree.checkout_root(identities_dir))
        return EthosExtDiagnostics.refresh(identities_dir, seal)

    @staticmethod
    def refresh(
        identities_dir: Path, guard: PathGuard = FOLLOW_SYMLINKS
    ) -> ExtScanOutcome:
        """Refresh every ``<handle>.ext/quarry.yaml`` under *identities_dir*.

        Each file passes *guard* before it is read or written. The operator's
        global tree keeps the default and follows symlinks (dotfile managers
        put them there); :meth:`refresh_vendored` hands in a sealed tree, and
        a refused path is recorded as that identity's failure.

        Only I/O, YAML, and decoding failures are recorded per identity — a
        non-UTF8 ext file raises ``UnicodeDecodeError`` (a ``ValueError``, not
        an ``OSError``) — so one bad file never stops the scan while a real
        bug still propagates. An absent directory yields an empty outcome.
        """
        buckets: dict[ExtWriteResult, list[str]] = {
            result: [] for result in ExtWriteResult
        }
        failed: list[ExtFailure] = []
        for handle, quarry_yaml in EthosExtDiagnostics._ext_files(identities_dir):
            try:
                result = EthosExtDiagnostics.write_session_context(
                    guard.check(quarry_yaml), handle
                )
            except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
                failed.append(ExtFailure(handle, str(exc)))
                continue
            buckets[result].append(handle)
        return ExtScanOutcome(
            updated=tuple(buckets[ExtWriteResult.UPDATED]),
            already_set=tuple(buckets[ExtWriteResult.ALREADY_SET]),
            no_collection=tuple(buckets[ExtWriteResult.NO_COLLECTION]),
            failed=tuple(failed),
        )

    @staticmethod
    def _ext_files(identities_dir: Path) -> list[tuple[str, Path]]:
        """Return ``(handle, quarry.yaml)`` for each ext dir that has the file."""
        if not identities_dir.is_dir():
            return []
        return [
            (ext_dir.name.removesuffix(".ext"), ext_dir / "quarry.yaml")
            for ext_dir in sorted(identities_dir.iterdir())
            if ext_dir.is_dir()
            and ext_dir.name.endswith(".ext")
            and (ext_dir / "quarry.yaml").is_file()
        ]

    @staticmethod
    def write_session_context(quarry_yaml: Path, handle: str) -> ExtWriteResult:
        """Write the current guide into one ``quarry.yaml`` if absent or stale.

        The raw text is edited as a line range and written back atomically;
        ``yaml.safe_load`` is used only to read ``memory_collection``, never to
        rewrite the file.
        """
        ext = AtomicFile(quarry_yaml)
        raw = ext.read()
        block = SessionContextBlock.locate(raw)
        if not block.needs_guide:
            return ExtWriteResult.ALREADY_SET
        data = yaml.safe_load(raw) or {}
        collection = data.get("memory_collection") if isinstance(data, dict) else None
        if not collection:
            return ExtWriteResult.NO_COLLECTION
        ext.replace(block.with_guide(handle, str(collection)))
        return ExtWriteResult.UPDATED
