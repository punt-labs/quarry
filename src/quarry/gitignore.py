"""Ensure quarry's own on-disk artifacts are excluded from a repo's ``.gitignore``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.atomic_file import AtomicFile
from quarry.file_lock import FILE_LOCK_GITIGNORE_GLOB, FileLock

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["CAPTURES_GITIGNORE_ENTRY", "QuarryGitignore"]

# The path quarry's CaptureWriter writes auto-captures under (session
# transcripts, web fetches — DES-036). A repo that never excludes it can
# commit raw, unscrubbed captures the moment the redaction pipeline lags a
# new capture kind or is bypassed — the root cause of the pkit-kcps leak.
CAPTURES_GITIGNORE_ENTRY = ".punt-labs/quarry/captures/"

# Every .gitignore line quarry's own file-writing needs, in write order.
# FILE_LOCK_GITIGNORE_GLOB excludes FileLock's own lock files (e.g.
# ".CLAUDE.md.punt-import.lock", "..gitignore.lock") -- see its docstring for why they
# are permanent, unignored artifacts without this.
_ENTRIES = (CAPTURES_GITIGNORE_ENTRY, FILE_LOCK_GITIGNORE_GLOB)


@final
class QuarryGitignore:
    """Own the ``.gitignore`` lines quarry's own file-writing needs.

    ``ensure`` mirrors :meth:`quarry.claude_import.ClaudeMdImport.register`:
    append each entry if no exact line is already present, no-op otherwise.
    Unlike the ``@``-import, a ``.gitignore`` has no fenced-code-block
    concept to shield against, so presence is a direct per-line match. A
    missing ``.gitignore`` is created by the same atomic write that appends
    the entries, so a repo with none yet still gets one. All entries are
    ensured under one :class:`FileLock` hold and one atomic write, so a
    concurrent ``enable`` sees either none or all of them.
    """

    __slots__ = ("_file",)

    _file: AtomicFile

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._file = AtomicFile(root / ".gitignore")
        return self

    @property
    def path(self) -> Path:
        """Return the managed ``.gitignore`` path."""
        return self._file.path

    def ensure(self) -> bool:
        """Append every missing entry in :data:`_ENTRIES`; return whether any were.

        Idempotent: a repo that already excludes every entry — from a prior
        ``enable`` or the user's own edit — is left byte-for-byte unchanged
        and this returns ``False``. Locked the same way
        ``ClaudeMdImport.register`` locks its host file, so two concurrent
        ``enable`` runs cannot lose one update to the other's overwrite.
        """
        with FileLock(self._file.path):
            content = self._file.read()
            lines = content.splitlines(keepends=True)
            present = {line.rstrip("\r\n") for line in lines}
            missing = [entry for entry in _ENTRIES if entry not in present]
            if not missing:
                return False
            eol = self._host_eol(content)
            if content and not content.endswith(("\n", "\r")):
                content += eol
            content += eol.join((*missing, ""))
            self._file.replace(content)
            return True

    @staticmethod
    def _host_eol(content: str) -> str:
        """Return the host's line ending from its FIRST newline: CRLF, lone CR, or LF.

        Mirrors :meth:`ClaudeMdImport._host_eol` — detecting the first
        terminator, not "contains anywhere", keeps a stray ``\\r`` later in the
        file from overriding a mostly-LF file's true ending.
        """
        cr = content.find("\r")
        lf = content.find("\n")
        if cr != -1 and (lf == -1 or cr < lf):
            return "\r\n" if content[cr + 1 : cr + 2] == "\n" else "\r"
        return "\n"
