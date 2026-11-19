"""Resolve or dispatch a directory's collection against the daemon registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.enable import RegistryClient

__all__ = ["Registrar"]


@final
class Registrar:
    """Resolve or dispatch a directory's collection against the daemon registry.

    Owns the ``RegistryClient`` so ``resolve`` reads the daemon's registry view
    (DES-031 I2) and dispatches a registration without threading the client
    through as a parameter — the client is state, not an argument (PY-OO-7).
    """

    __slots__ = ("_client",)

    _client: RegistryClient

    def __new__(cls, client: RegistryClient) -> Self:
        """Bind directory registration dispatch to a registry-capable client."""
        self = super().__new__(cls)
        self._client = client
        return self

    def resolve(self, directory: Path, collection_override: str) -> tuple[str, bool]:
        """Reuse the covering registration, or dispatch a new one to the daemon.

        Returns (collection_name, created).  Raises ValueError when *directory*
        is a child of an existing registration (sessions there use the parent's
        collection automatically).
        """
        from quarry.api import RegisterRequest  # noqa: PLC0415
        from quarry.registrations import Registrations  # noqa: PLC0415

        view = Registrations.from_list(self._client.list_registrations())
        covering = view.covering(directory)
        if covering is not None:
            if covering.directory == str(directory):
                return covering.collection, False
            msg = (
                f"This directory is already covered by the registration at "
                f"{covering.directory} (collection: {covering.collection}). "
                f"Sessions here use that collection automatically. No action needed."
            )
            raise ValueError(msg)

        # Re-adopt: if THIS directory owns an archived (keep-data) collection,
        # reuse its name so the daemon's register re-adopts the kept chunks and
        # its rescan auto-freshens them. A different directory owns no archive
        # here, so it falls through to a fresh unique name that avoids every
        # archived name (I7).
        archived = view.archived_collection_for(directory)
        name = collection_override or archived or view.unique_collection_name(directory)
        # Fire-and-forget: the daemon re-guards the path on its own filesystem
        # and writes the registry row as a background task.
        self._client.register(
            RegisterRequest(directory=str(directory), collection=name)
        )
        return name, True
