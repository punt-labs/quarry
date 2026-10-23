"""The rules that refuse a directory registration, apart from the registry's SQL."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.collection_namer import CollectionNamer
from quarry.refusal import RegistrationRefusedError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from quarry.collection_marker_store import RetainedMarker
    from quarry.sync_registry import DirectoryRegistration


@final
class RegistrationRules:
    """Decide that a registration is refused; each rule raises or returns nothing.

    Pure decisions over values the registry has already read, so they are tested
    without a database and the registry keeps only the reads and writes.
    """

    __slots__ = ()

    @staticmethod
    def is_ancestor_of(ancestor: Path, descendant: Path) -> bool:
        """Return True if *ancestor* is a strict ancestor of *descendant*.

        Both paths should be resolved (absolute, no symlinks); strict inequality
        means a path is not its own ancestor.
        """
        return ancestor != descendant and descendant.is_relative_to(ancestor)

    @classmethod
    def refuse_if_covered(
        cls, registrations: Iterable[DirectoryRegistration], resolved: Path
    ) -> None:
        """Refuse *resolved* when an existing registration already covers it."""
        for reg in registrations:
            if cls.is_ancestor_of(Path(reg.directory).resolve(), resolved):
                msg = (
                    "directory covered by a parent registration; the parent "
                    "registration already indexes it; deregister the parent to "
                    "register this directory on its own "
                    "(the conflicting registration is named in the quarry logs)"
                )
                raise RegistrationRefusedError(msg).about(
                    directory=reg.directory, collection=reg.collection
                )

    @staticmethod
    def refuse_unsafe_name(resolved: Path, collection: str) -> None:
        """Refuse a new collection name holding a character a name may not hold.

        The name is shown to people and models and quoted into command lines, so
        a quote, a line break or a shell metacharacter in it is refused here, once,
        for every door a registration comes through.  The text is fixed.
        """
        if CollectionNamer.safe(collection) == collection and collection:
            return
        msg = (
            "collection name may only hold letters, digits, '.', '_' and '-'; "
            "choose another collection name"
        )
        raise RegistrationRefusedError(msg).about(
            directory=str(resolved), collection=collection, renamable=True
        )

    @staticmethod
    def refuse_foreign_retained(
        marker: RetainedMarker | None, resolved: Path, collection: str
    ) -> None:
        """Refuse re-using an archived collection name from a different directory.

        A keep-data deregister archives a collection's chunks under a retained
        marker tagged with its original directory.  Only that same directory may
        re-register the name — re-adopting its own kept chunks.  A different
        directory re-using the name would silently merge two projects' data into
        one collection (the opposite of keep-data's promise), so it is refused.

        A legacy marker with no recorded origin (``original_directory`` NULL,
        written before the column existed) matches no directory and is refused for
        every directory: without a verifiable origin the safe default is to never
        merge.  The refusal's text is fixed; the names ride its structured fields,
        for the redacted log line.
        """
        if marker is None or marker.original_directory == str(resolved):
            return
        msg = (
            "collection holds archived data from another directory; choose "
            "another collection name, or re-register the directory that owns it "
            "(the conflicting registration is named in the quarry logs)"
        )
        origin = marker.original_directory or "an unknown directory"
        raise RegistrationRefusedError(msg).about(
            directory=origin,
            collection=collection,
            detail=f"kept from {origin}",
            renamable=True,
        )

    @staticmethod
    def integrity_refusal(
        existing: tuple[str, str] | None, resolved: Path, collection: str
    ) -> RegistrationRefusedError:
        """Return the refusal for an INSERT that hit a uniqueness constraint.

        *existing* is the ``(directory, collection)`` row that collided, if any.
        """
        if existing and existing[0] == str(resolved):
            msg = (
                "directory already registered; run quarry sync, or deregister it first "
                "(the conflicting registration is named in the quarry logs)"
            )
            held = f"held by collection {existing[1]!r}"
            return RegistrationRefusedError(msg).about(
                directory=str(resolved), collection=collection, detail=held
            )
        msg = (
            "collection name already in use; choose another collection name "
            "(the conflicting registration is named in the quarry logs)"
        )
        return RegistrationRefusedError(msg).about(
            directory=str(resolved), collection=collection, renamable=True
        )
