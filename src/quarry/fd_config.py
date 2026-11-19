"""The daemon's file-descriptor envelope: the two halves that size RLIMIT_NOFILE.

The envelope has two halves applied at two different moments. :class:`FdEnvelope`
raises the SOFT ``RLIMIT_NOFILE`` in-process at daemon start; :class:`FdServiceLimits`
bakes the service-manager SOFT/HARD ceiling into the launchd plist / systemd unit
BEFORE quarryd is spawned. The two are co-located because they are one design: the
service manager (the only actor that can raise a non-root process's HARD limit)
grants the ceiling, and ``FdEnvelope`` then lifts the soft off it at runtime. See
DES-046.

The daemon inherits launchd/systemd's soft ``RLIMIT_NOFILE`` (256 on macOS) and,
post-DES-045, holds one persistent LanceDB connection per roster database. Each
connection's reader-recycler bounds its own descriptors, but the aggregate across
a ~21-collection roster sits above a 256 limit sized for two connections, so the
daemon walks into ``EMFILE`` over long uptime. :class:`FdEnvelope` raises the soft
limit toward a configured target at daemon start — the sibling of
``ThreadConfig``'s CPU/thread envelope (DES-032), applied at the same seam before
the first ``lancedb.connect``. See DES-046.

The raise is fail-safe: a missing ``resource`` module, a raising ``getrlimit``,
or a raising ``setrlimit`` each degrade to a single logged line and the inherited
limit — the whole point is to survive, so a platform quirk must never crash the
daemon at start. A target above the hard limit is not a degrade: ``apply`` clamps
to the hard limit and successfully raises the soft limit to it. It never lowers
an already higher inherited soft limit, so an operator's ``LimitNOFILE=65536`` is
honored.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Self, final

from quarry.config import DEFAULT_FD_LIMIT, Settings

logger = logging.getLogger(__name__)

# ``resource`` is POSIX-only and absent on platforms without rlimits (e.g.
# Windows). Import it optionally, mirroring fd_headroom.py, so importing this
# module never crashes there; a missing module degrades ``apply`` to a no-op.
resource: ModuleType | None
try:
    resource = importlib.import_module("resource")
except ImportError:  # pragma: no cover - POSIX-only; non-POSIX import must not fail
    resource = None


@final
class FdEnvelope:
    """The daemon's file-descriptor budget: raise the soft ``RLIMIT_NOFILE`` once.

    Built with the target soft limit resolved from configuration
    (``Settings.fd_limit``), not a module constant, so the budget is
    env-overridable via ``QUARRY_FD_LIMIT`` at the one place it is applied.
    """

    __slots__ = ("_target",)

    _target: int

    def __new__(cls, *, target: int) -> Self:
        """Select the desired soft descriptor limit without changing process limits."""
        self = super().__new__(cls)
        self._target = target
        return self

    def apply(self) -> str:
        """Raise the soft fd limit toward the target; return the effective state.

        Fail-safe at every boundary (Bug-class-2): a missing ``resource`` module,
        a ``getrlimit`` that raises, or a ``setrlimit`` that raises degrades to a
        single logged line and the inherited limit — never a crash. A target above
        the hard limit is clamped to the hard limit and the soft limit is raised to
        it (a success, not a degrade). Never LOWERS an already-higher inherited soft
        limit (the floor is the inherited soft), so a higher operator/systemd
        ``LimitNOFILE`` is honored.
        """
        rlimits = resource
        if rlimits is None:
            state = "unavailable (no resource module on this platform)"
            logger.info("fd envelope: %s", state)
            return state
        try:
            soft, hard = rlimits.getrlimit(rlimits.RLIMIT_NOFILE)
        except OSError as exc:
            state = f"unchanged (getrlimit failed: {exc})"
            logger.warning("fd envelope: %s", state)
            return state
        unbounded = hard == rlimits.RLIM_INFINITY
        effective = self._target if unbounded else min(self._target, hard)
        if soft >= effective:
            state = f"unchanged (inherited soft {soft} already >= target {effective})"
            logger.info("fd envelope: %s", state)
            return state
        try:
            rlimits.setrlimit(rlimits.RLIMIT_NOFILE, (effective, hard))
        except (OSError, ValueError) as exc:
            state = f"unchanged (setrlimit to {effective} failed: {exc})"
            logger.warning("fd envelope: %s", state)
            return state
        state = f"raised soft {soft} -> {effective} (hard {hard})"
        logger.info("fd envelope: %s", state)
        return state


# The hard RLIMIT_NOFILE ceiling the service manager grants quarryd before spawn.
# A freshly BOOTSTRAPPED launchd agent inherits hard=256, and a non-root process
# cannot raise its own hard limit — so the in-daemon FdEnvelope (DES-046) clamps to
# 256 and the daemon walks into EMFILE. The service manager is the one actor that
# CAN raise the hard limit, so the generated plist/unit bake this generous ceiling;
# FdEnvelope then lifts the SOFT limit toward QUARRY_FD_LIMIT at runtime within it.
# 65536 is ~8x the 8192 default soft target — years of roster-growth headroom — and
# sits well under macOS kern.maxfilesperproc (~92160) and any systemd hard limit.
_SERVICE_FD_HARD = 65536


@final
class FdServiceLimits:
    """The RLIMIT_NOFILE window the service manager bakes into the plist/unit.

    The service manager sets the descriptor limits BEFORE spawning quarryd because
    it — unlike the non-root daemon — can raise the hard limit above the 256 a fresh
    launchd bootstrap inherits.  The hard ceiling is fixed and generous
    (``_SERVICE_FD_HARD``); the soft is the configured target (``Settings.fd_limit``,
    ``QUARRY_FD_LIMIT``-overridable), floored UNCONDITIONALLY at the safe default
    (``DEFAULT_FD_LIMIT``) so the override can only RAISE it, and capped ABOVE at the
    hard ceiling so the file stays valid (a soft above the hard is rejected).  The
    constructor rejects a ``hard`` below the floor — clamping against a too-low
    ceiling would silently re-admit a soft below ``DEFAULT_FD_LIMIT``, defeating it.

    ``QUARRY_FD_LIMIT`` is applied at INSTALL time, not at runtime: ``quarry install``
    bakes the soft limit into the plist ``SoftResourceLimits`` / systemd
    ``LimitNOFILE`` from ``Settings().fd_limit``.  It is in neither the plist
    ``EnvironmentVariables`` nor the systemd ``EnvironmentFile``, so the running
    daemon's :class:`FdEnvelope` reads only the default — changing the override
    therefore requires re-running ``quarry install`` (a reinstall), not a mere daemon
    restart.  FdEnvelope lifts the soft off the baked ceiling at runtime and never
    lowers an already-higher inherited soft limit.
    """

    __slots__ = ("_hard", "_soft")

    _soft: int
    _hard: int

    def __new__(cls, *, soft: int, hard: int) -> Self:
        """Clamp a service soft limit within the safe floor and requested ceiling.

        Raises:
            ValueError: The hard ceiling is below Quarry's safe default.
        """
        if hard < DEFAULT_FD_LIMIT:
            msg = (
                f"hard fd ceiling {hard} is below the safe default "
                f"{DEFAULT_FD_LIMIT} — the soft floor could not hold"
            )
            raise ValueError(msg)
        self = super().__new__(cls)
        # Floor to the safe default and cap at the hard ceiling, so QUARRY_FD_LIMIT
        # may only RAISE the soft limit — never lower it into the EMFILE range
        # (config._coerce_fd_limit accepts any positive int, e.g. 100).  The guard
        # above keeps the floor unconditional: max() always wins over a valid hard.
        self._soft = min(max(soft, DEFAULT_FD_LIMIT), hard)
        self._hard = hard
        return self

    @classmethod
    def from_settings(cls) -> Self:
        """Build the ceiling from config: soft = ``QUARRY_FD_LIMIT``, hard = fixed."""
        return cls(soft=Settings().fd_limit, hard=_SERVICE_FD_HARD)

    @property
    def soft(self) -> int:
        """Return the soft descriptor limit baked in (clamped to the hard ceiling)."""
        return self._soft

    @property
    def hard(self) -> int:
        """Return the fixed hard descriptor ceiling the service manager grants."""
        return self._hard

    def launchd_fragment(self) -> str:
        """Return the ``SoftResourceLimits`` + ``HardResourceLimits`` plist dicts.

        Indented to match the sibling ``EnvironmentVariables`` block and ending in
        the 8-space pad the next top-level ``<key>`` rides on, so the outer
        ``textwrap.dedent`` in ``_launchd_plist_content`` renders valid XML.
        """
        return (
            "<key>SoftResourceLimits</key>\n"
            "        <dict>\n"
            "            <key>NumberOfFiles</key>\n"
            f"            <integer>{self._soft}</integer>\n"
            "        </dict>\n"
            "        <key>HardResourceLimits</key>\n"
            "        <dict>\n"
            "            <key>NumberOfFiles</key>\n"
            f"            <integer>{self._hard}</integer>\n"
            "        </dict>\n"
            "        "
        )

    def systemd_directive(self) -> str:
        """Return the ``LimitNOFILE=<soft>:<hard>`` directive for the [Service] block.

        systemd's ``LimitNOFILE`` takes ``soft:hard`` and raises the daemon's hard
        limit above the login-session default, the Linux analogue of the launchd
        ``HardResourceLimits`` ceiling.
        """
        return f"LimitNOFILE={self._soft}:{self._hard}"
