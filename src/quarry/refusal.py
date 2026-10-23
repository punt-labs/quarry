"""The base type of an expected refusal whose message is safe to show a client."""

from __future__ import annotations

from typing import Self


class RefusalError(ValueError):
    """Raised for an expected "no": the message is a stable reason, safe to show.

    A refusal is an answer, not a fault.  Its message never embeds a URL, a
    credential or a caller-supplied name, so it may be logged at WARNING without
    a traceback and returned to a client verbatim.  Anything that is not a
    :class:`RefusalError`, a stray ``ValueError`` included, fails closed: the client
    sees only the exception's class.

    What an operator needs to know that the client must not be told (which
    collection, which directory) rides in :attr:`detail`, which only the redacted
    log line carries.  ``tests/test_refusal_raise_sites.py`` pins which raise sites
    interpolate anything at all.
    """

    _detail: str = ""

    def noting(self, detail: str) -> Self:
        """Attach operator-only *detail* and return the refusal, ready to raise."""
        self._detail = detail
        return self

    @property
    def detail(self) -> str:
        """Return the operator-only context for the log line (never the client's)."""
        return self._detail


class RegistrationRefusedError(RefusalError):
    """Raised when a directory registration is refused: nested, duplicate or taken.

    The message is fixed text.  The directory and collection involved are kept as
    structured fields, and :attr:`~RefusalError.detail` says more, for the log.
    """

    _directory: str = ""
    _collection: str = ""
    _renamable: bool = False

    def about(
        self,
        *,
        directory: str,
        collection: str,
        detail: str = "",
        renamable: bool = False,
    ) -> Self:
        """Record the directory and collection involved, and return the refusal.

        *renamable* is whether registering under another collection name would
        get past it.
        """
        self._directory = directory
        self._collection = collection
        self._renamable = renamable
        return self.noting(
            f"directory={directory!r} collection={collection!r} {detail}"
        )

    @property
    def directory(self) -> str:
        """Return the directory the refusal concerns (for the log, not the client)."""
        return self._directory

    @property
    def collection(self) -> str:
        """Return the collection the refusal concerns (for the log, not the client)."""
        return self._collection

    @property
    def renamable(self) -> bool:
        """Return whether another collection name would get past this refusal."""
        return self._renamable
