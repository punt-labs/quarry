"""Add or remove one bare ``@``-import line in a ``CLAUDE.md`` host file."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.atomic_file import AtomicFile
from quarry.fence import FenceScanner
from quarry.file_lock import FileLock

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ClaudeMdImport"]


@final
class ClaudeMdImport:
    """Reconcile a single ``@``-import line inside a user-owned ``CLAUDE.md``.

    The only mutation any tool may make to a ``CLAUDE.md`` is to add or remove
    one bare ``@``-import line (tool-enable-disable.md § 2.1). This class owns
    exactly that mutation for one host file and one import string:

    * **Serialized** — an exclusive ``flock`` on a sibling ``.lock`` file wraps
      the whole read-modify-write, so two concurrent ``enable`` runs cannot lose
      one update (§ 2.4). The atomic rename below prevents a torn file but not a
      lost update; the lock closes that gap, which the vox reference lacks.
    * **Idempotent, terminator-insensitive** — presence is decided by matching
      the canonical line against each host line *net of its terminator*, so a
      CRLF host does not carry a spurious ``\\r`` that defeats a byte-exact
      compare. ``register`` appends only if absent; ``prune`` removes every
      match (collapsing an accidental duplicate to zero).
    * **Top-level only** — a match inside a fenced (```` ``` ````/``~~~``) or an
      indented (tab or ≥4 spaces) code block is ignored by both the presence
      scan and the removal, because Claude Code resolves ``@``-imports only at
      the top level.
    * **Byte-preserving, host EOL** — every byte outside the single import line
      is identical before and after across LF, CRLF, and lone-CR; the appended
      line uses the host file's existing EOL. Delegated to :class:`AtomicFile`.
    """

    __slots__ = ("_file",)

    _file: AtomicFile

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._file = AtomicFile(path)
        return self

    @property
    def path(self) -> Path:
        """Return the managed ``CLAUDE.md`` path."""
        return self._file.path

    def register(self, import_line: str) -> bool:
        """Append *import_line* if no top-level copy is present. Return True if written.

        Ensures separation from the user's last line: a host file not ending in
        a newline gets one (in the host EOL) before the import is appended, so
        the import is never glued to the user's prose.
        """
        self._validate_import_line(import_line)
        with FileLock.shared(self._file.path):
            content = self._file.read()
            lines = content.splitlines(keepends=True)
            hits, fence_open = self._scan_toplevel(lines, import_line)
            if hits:
                return False
            if fence_open:
                # The host ends inside an unterminated code fence, so an appended
                # line would land inside that block — inert, not a top-level
                # import. Refuse rather than write an import Claude Code cannot
                # resolve: enable() runs register before the marker, so raising
                # here leaves neither present, never the marker-without-effective-
                # import state the § 2.11 biconditional forbids.
                msg = (
                    f"cannot register import: {self._file.path} ends inside an "
                    "unterminated code fence"
                )
                raise ValueError(msg)
            eol = self._host_eol(content)
            if content and not content.endswith(("\n", "\r")):
                content += eol
            self._file.replace(content + import_line + eol)
            return True

    def prune(self, import_line: str) -> bool:
        """Remove every top-level copy of *import_line*. Return True if written.

        A copy inside a code block is left in place; only top-level imports are
        Claude Code imports, so only those are the tool's to remove.
        """
        self._validate_import_line(import_line)
        with FileLock.shared(self._file.path):
            content = self._file.read()
            lines = content.splitlines(keepends=True)
            matches, _ = self._scan_toplevel(lines, import_line)
            hits = set(matches)
            if not hits:
                return False
            kept = [line for i, line in enumerate(lines) if i not in hits]
            self._file.replace("".join(kept))
            return True

    @staticmethod
    def _validate_import_line(import_line: str) -> None:
        """Raise ``ValueError`` unless *import_line* is a lone top-level ``@`` line.

        The line is spliced verbatim into the host file, so untrusted text is
        validated at this boundary (PY-EH-1): a padded, multi-line, or non-``@``
        line would inject a blank line, a second import, or inert markdown, and
        a padded line would never match on a later prune.
        """
        if not import_line or import_line.isspace():
            msg = "import line must be non-empty"
            raise ValueError(msg)
        if "\n" in import_line or "\r" in import_line:
            msg = f"import line must be a single line: {import_line!r}"
            raise ValueError(msg)
        if import_line != import_line.strip():
            msg = f"import line has leading/trailing whitespace: {import_line!r}"
            raise ValueError(msg)
        if not import_line.startswith("@"):
            msg = f"import line must begin with '@': {import_line!r}"
            raise ValueError(msg)

    @staticmethod
    def _scan_toplevel(lines: list[str], import_line: str) -> tuple[list[int], bool]:
        """Return top-level matches of *import_line* and the EOF fence state.

        The first element lists the indices of *lines* equal to *import_line* net
        of terminator; a line shielded by a Markdown code fence or an indented
        code block is never a match, because only a top-level ``@``-import is a
        Claude Code import (§ 2.4). The second element is whether the scan ends
        inside an unterminated fence — the state ``register`` must refuse, since
        an appended line would then be code, not a top-level import.

        :class:`FenceScanner` tracks fence state across the scan, honouring
        CommonMark's same-character close and indentation rules.
        """
        scanner = FenceScanner()
        hits = [
            i
            for i, raw in enumerate(lines)
            if not scanner.shields(raw) and raw.rstrip("\r\n") == import_line
        ]
        return hits, scanner.is_open

    @staticmethod
    def _host_eol(content: str) -> str:
        """Return the host's line ending from its FIRST newline: CRLF, lone CR, or LF.

        Detecting the first terminator, not "contains anywhere", keeps a stray
        ``\\r`` inside a code block from overriding a mostly-LF file's true ending
        and appending the import with the wrong line separator.
        """
        cr = content.find("\r")
        lf = content.find("\n")
        if cr != -1 and (lf == -1 or cr < lf):
            return "\r\n" if content[cr + 1 : cr + 2] == "\n" else "\r"
        return "\n"
