"""Read and rewrite a text file atomically, preserving bytes, mode, and symlinks."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Self, final

__all__ = ["AtomicFile"]


@final
class AtomicFile:
    """A text file rewritten without ever corrupting the user's content.

    Reads preserve bytes verbatim (``newline=""``), so a read/write round-trip
    keeps LF, CRLF, and lone-CR endings byte-identical. Each write lands
    atomically: a temp file in the target's own directory is flushed,
    ``fsync``-ed, and ``os.replace``-d over the target, so an interrupted write
    (SIGKILL, power loss) leaves the original untouched rather than truncated. A
    symlinked path is followed to its real file so the rename updates the file
    rather than clobbering the link. The replacement's mode defaults to the
    target's current mode (a brand-new file gets :data:`_NEW_FILE_MODE`), or a
    caller may force one via ``replace(..., mode=...)``.

    Ported from ``punt_vox.atomic_file`` (tool-enable-disable.md § 2.4 names the
    vox artifact as the reference for the atomic / symlink / byte-preserving /
    deterministic contract); the serialization lock the standard also mandates
    lives one layer up in :class:`quarry.claude_import.ClaudeMdImport`, which
    owns the read-modify-write this file only executes the write half of.
    """

    __slots__ = ("_path",)

    _path: Path

    # Temp files share the target's directory so ``os.replace`` is a
    # same-filesystem atomic rename; each is named after its target so a
    # leftover temp is self-documenting (``.CLAUDE.md.<rand>.tmp``).
    _TMP_SUFFIX = ".tmp"
    # A brand-new file's default mode -- predictable regardless of umask, unlike
    # the 0600 ``mkstemp`` stamps on the temp or the ``0666 & ~umask`` a plain
    # ``open()`` would produce.
    _NEW_FILE_MODE = 0o644

    def __new__(cls, path: Path) -> Self:
        """Bind byte-preserving atomic text operations to a path without opening it."""
        self = super().__new__(cls)
        self._path = path
        return self

    @property
    def path(self) -> Path:
        """Return the managed path (the symlink itself when the path is one)."""
        return self._path

    def read(self) -> str:
        r"""Return the file's text verbatim, or ``""`` when it does not exist.

        ``newline=""`` disables universal-newline translation; without it
        ``read_text`` would rewrite every ``\r\n`` and lone ``\r`` to ``\n``,
        silently normalizing a CRLF or old-Mac file to LF before the caller has
        even parsed it. Reading bytes as-is keeps a write-then-read round-trip
        byte-identical for LF, CRLF, and lone-CR alike.
        """
        if not self._path.is_file():
            return ""
        return self._path.read_text(encoding="utf-8", newline="")

    def replace(self, text: str, *, mode: int | None = None) -> None:
        """Replace the file's contents with *text* atomically.

        Write *text* to a temp file in the target's own directory, flush and
        ``fsync`` it, then ``Path.replace`` it over the target. ``Path.replace``
        wraps ``os.replace`` -- an atomic rename on POSIX -- so an interrupted
        write leaves the original untouched rather than truncated.

        ``mode`` forces the replacement's permission bits when set; ``None``
        keeps the default policy of preserving an existing target's mode, else
        :data:`_NEW_FILE_MODE`.
        """
        target = self._write_target()
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        # Name the temp after its target: ``.CLAUDE.md.<rand>.tmp``.
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f".{target.name}.", suffix=self._TMP_SUFFIX
        )
        tmp = Path(tmp_name)
        try:
            # fdopen takes ownership of the fd first, so the ``with`` below
            # closes it on every path. If fdopen itself raises, the raw fd is
            # closed explicitly -- else a repeated write leaks a descriptor.
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)
            # Suppress a raising unlink so the original fdopen error propagates,
            # not the cleanup failure -- parity with the main path below.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # ``Path.replace`` preserves the temp's 0600, so stamp the intended
            # mode before the rename or an existing 0644 file would become 0600.
            tmp.chmod(self._replacement_mode(target, mode))
            tmp.replace(target)
        except BaseException:
            # Any pre-rename failure (of any exception type) orphans the temp;
            # unlink it, suppressing a raising unlink so the bare ``raise``
            # surfaces the real cause. Unreachable once ``replace`` succeeds.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise

    def _write_target(self) -> Path:
        """Return the real path the atomic replace must rename onto.

        When ``self._path`` is a symlink -- common with dotfile managers
        (chezmoi, GNU stow) -- ``os.replace(tmp, link)`` would replace the *link*
        with a regular file, breaking the setup. Resolving to the link's
        destination renames onto the real file and preserves the link;
        ``resolve()`` also settles a symlinked parent so the temp shares the
        target's filesystem and the rename stays atomic. A non-symlink path is
        returned unchanged.

        ``strict=False`` is required: a dotfile manager may point the link at a
        target that does not exist yet (first ``enable`` on a fresh checkout).
        ``resolve(strict=True)`` would raise ``FileNotFoundError`` there; the
        non-strict form still normalizes the path so the create lands on the
        link's destination.
        """
        if self._path.is_symlink():
            return self._path.resolve(strict=False)
        return self._path

    def _replacement_mode(self, target: Path, mode: int | None) -> int:
        """Return the permission bits to stamp on the replacement file.

        A forced *mode* wins outright. Otherwise preserve *target*'s current
        mode so a replace never changes how the user's file is exposed, falling
        back to :data:`_NEW_FILE_MODE` for a brand-new file. Called before the
        rename, while the target (if any) still exists.
        """
        # ``mode is None`` is the documented "use default policy" signal, not a
        # missing value; an explicit int forces that mode.
        if mode is not None:
            return mode
        if target.is_file():
            return stat.S_IMODE(target.stat().st_mode)
        return self._NEW_FILE_MODE
