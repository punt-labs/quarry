"""The collection a directory enables under, and how it was found."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from quarry.api import RegistrationInfo

__all__ = ["Resolution"]


@dataclass(frozen=True, slots=True)
class Resolution:
    """The collection a directory enables under, and how it was found.

    ``created`` is true when a new registration was dispatched.  ``covered_by``
    is the parent registration's directory when one covers the directory (the
    directory then enables under the parent's collection and registers nothing),
    else empty.
    """

    collection: str
    created: bool
    covered_by: str = ""

    @classmethod
    def under(cls, covering: RegistrationInfo, override: str) -> Self:
        """Reuse the registration *covering* a child directory.

        Raises ValueError when *override* names a different collection than the
        covering registration's: the override cannot take effect there.
        """
        if override not in ("", covering.collection):
            msg = (
                f"This directory is already covered by the registration at "
                f"{covering.directory} (collection: {covering.collection}); "
                f"--collection {override} cannot apply here."
            )
            raise ValueError(msg)
        return cls(covering.collection, created=False, covered_by=covering.directory)
