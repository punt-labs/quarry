"""Refresh the quarry memory guide in each ethos identity's ``quarry.yaml`` ext."""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

import yaml

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

    Every read and every write of an ext file goes through the guard the
    instance was built with. The default follows symlinks — the operator's
    global tree, where dotfile managers put them on purpose.
    :meth:`refresh_vendored` builds a sealed instance for a checkout's
    vendored tree, so a committed link can neither feed the parser nor
    receive the guide.
    """

    __slots__ = ("_guard",)

    _guard: PathGuard

    def __new__(cls, guard: PathGuard = FOLLOW_SYMLINKS) -> Self:
        self = super().__new__(cls)
        self._guard = guard
        return self

    @classmethod
    def configure(cls, identities_dir: Path | None = None) -> CheckResult:
        """Best-effort install step: refresh the guide across the global tree.

        ``quarry install`` has no repo context, so this touches the global
        identities only; ``quarry enable`` handles a repo's vendored tree.
        ``None`` means the operator's global tree.
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
        outcome = cls().refresh(identities_dir)
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

    @classmethod
    def refresh_vendored(cls, identities_dir: Path) -> ExtScanOutcome:
        """Refresh a checkout's vendored ``identities/``, sealed at the checkout root.

        The vendored tree is cloned content: a committed symlink under it could
        redirect the guide write onto a file outside the repo (the operator's
        global ext, with an attacker-chosen handle in the guide). Sealing at
        the checkout root refuses any symlink component below it — inside the
        read and inside the write, not by a check made before either; the
        root itself is the operator's choice and may be one.
        """
        return cls(SealedTree(EthosTree.checkout_root(identities_dir))).refresh(
            identities_dir
        )

    def refresh(self, identities_dir: Path) -> ExtScanOutcome:
        """Refresh every ``<handle>.ext/quarry.yaml`` under *identities_dir*.

        Each file is read and rewritten through this instance's guard; a
        refused path is recorded as that identity's failure.

        Only I/O, YAML, and decoding failures are recorded per identity — a
        non-UTF8 ext file raises ``UnicodeDecodeError`` (a ``ValueError``, not
        an ``OSError``) — so one bad file never stops the scan while a real
        bug still propagates. An absent directory yields an empty outcome.
        """
        buckets: dict[ExtWriteResult, list[str]] = {
            result: [] for result in ExtWriteResult
        }
        failed: list[ExtFailure] = []
        for handle, quarry_yaml in self._ext_files(identities_dir):
            try:
                result = self.write_session_context(quarry_yaml, handle)
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
        """Return ``(handle, quarry.yaml)`` for each ext dir that has the file.

        A listing only: the entries are candidates, and the guard decides
        inside each read and write whether one may be used.
        """
        if not identities_dir.is_dir():
            return []
        return [
            (ext_dir.name.removesuffix(".ext"), ext_dir / "quarry.yaml")
            for ext_dir in sorted(identities_dir.iterdir())
            if ext_dir.is_dir()
            and ext_dir.name.endswith(".ext")
            and (ext_dir / "quarry.yaml").is_file()
        ]

    def write_session_context(self, quarry_yaml: Path, handle: str) -> ExtWriteResult:
        """Write the current guide into one ``quarry.yaml`` if absent or stale.

        The raw text is edited as a line range and written back atomically
        through the guard, which preserves the file's mode; ``yaml.safe_load``
        is used only to read ``memory_collection``, never to rewrite the file.
        """
        raw = self._guard.read_text(quarry_yaml)
        block = SessionContextBlock.locate(raw)
        if not block.needs_guide:
            return ExtWriteResult.ALREADY_SET
        data = yaml.safe_load(raw) or {}
        collection = data.get("memory_collection") if isinstance(data, dict) else None
        if not collection:
            return ExtWriteResult.NO_COLLECTION
        self._guard.write_text(quarry_yaml, block.with_guide(handle, str(collection)))
        return ExtWriteResult.UPDATED
