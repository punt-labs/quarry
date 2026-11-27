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
    def reusing(cls, covering: RegistrationInfo, directory: str, override: str) -> Self:
        """Reuse the registration *covering* *directory*, itself or a parent.

        ``covered_by`` is set only when the covering registration is a parent's.
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
        parent = "" if covering.directory == directory else covering.directory
        return cls(covering.collection, created=False, covered_by=parent)
