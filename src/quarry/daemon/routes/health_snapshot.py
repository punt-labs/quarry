"""Assemble the ``/health`` body: liveness plus the daemon's own resource state."""

from __future__ import annotations

from importlib.metadata import version
from typing import Self, final

from quarry.api import API_VERSION
from quarry.api.health import FdHealth, FinalizeHealth
from quarry.fd_headroom import FdHeadroom
from quarry.finalize_ledger import FinalizeLedgers

# The running package version, read once at import for the health snapshot.
_QUARRY_VERSION = version("punt-quarry")


@final
class HealthSnapshot:
    """One ``/health`` reading, built where the daemon's real state lives."""

    __slots__ = ("_state", "_uptime_seconds")

    _uptime_seconds: float
    _state: str

    def __new__(cls, *, uptime_seconds: float, state: str) -> Self:
        """Freeze uptime and readiness for one health response."""
        self = super().__new__(cls)
        self._uptime_seconds = uptime_seconds
        self._state = state
        return self

    def payload(self) -> dict[str, object]:
        """Return the JSON body: liveness, versions, fd headroom, finalize ledger."""
        fd = self._fd_headroom()
        return {
            "status": "ok",
            "uptime_seconds": round(self._uptime_seconds, 1),
            "state": self._state,
            "api_version": API_VERSION,
            "quarry_version": _QUARRY_VERSION,
            "fd": fd.model_dump() if fd is not None else None,
            "finalize": FinalizeHealth(
                databases=list(FinalizeLedgers.snapshot())
            ).model_dump(),
        }

    @staticmethod
    def _fd_headroom() -> FdHealth | None:
        """Sample the daemon's own open-fd headroom as an :class:`FdHealth`.

        Runs in the daemon process, so the sample *is* the resident daemon's real
        descriptor state — the number doctor must report, not the short-lived
        CLI's shell ulimit. A sample that raises (``EMFILE`` mid-scan or a
        platform with no fd directory) yields ``None`` so the health endpoint
        never 500s on the very exhaustion it exists to surface; doctor renders it
        as a degraded advisory. Returning the model makes :class:`FdHealth` the
        single owner of the fd wire shape (bug-class-3).
        """
        try:
            headroom = FdHeadroom.sample()
        except OSError:
            return None
        return FdHealth(open_fds=headroom.open_fds, soft_limit=headroom.soft_limit)
