"""The paths quarry keeps under the home directory, resolved when asked."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast, final

if TYPE_CHECKING:
    from collections.abc import Callable


class HomeUnresolvedError(RuntimeError):
    """The home directory cannot be determined, or is not a usable absolute path.

    Raised only by :class:`QuarryHome` (and so by what builds a path through it).
    The entry points match on this type, never on a message, to turn it into one
    actionable line.
    """


@final
class QuarryHome:
    """Name the files under ``~`` without resolving ``~`` until a path is wanted.

    A module that resolved the home directory when it was imported would fail to
    import on a host where it cannot be resolved (no ``HOME``, no passwd entry),
    and so would every command, the daemon and the hooks.  Each path here is
    built at the call, so only the code that really needs the home directory
    fails, with :class:`HomeUnresolvedError`.  Tests substitute these methods (or
    ``Path.home``) rather than module constants.
    """

    __slots__ = ()

    @staticmethod
    def home() -> Path:
        """Return the home directory, or raise :class:`HomeUnresolvedError`.

        An empty ``HOME`` resolves to the working directory and a relative one to
        a path that moves with it, so neither is a home: every path built under it
        would land wherever the process happens to be.
        """
        try:
            home = Path.home()
        except RuntimeError as exc:
            msg = "cannot determine the home directory"
            raise HomeUnresolvedError(msg) from exc
        if not home.is_absolute() or home == Path():
            msg = "the home directory is not an absolute path"
            raise HomeUnresolvedError(msg)
        return home

    @classmethod
    def root(cls) -> Path:
        """Return quarry's per-user directory."""
        return cls.home() / ".punt-labs" / "quarry"

    @classmethod
    def data_root(cls) -> Path:
        """Return the default data root, used when ``QUARRY_ROOT`` is unset."""
        return cls.root() / "data"

    @classmethod
    def log_root(cls) -> Path:
        """Return the default log directory, used when ``QUARRY_LOG_DIR`` is unset."""
        return cls.root() / "logs"

    @staticmethod
    def under_root(*parts: str) -> Callable[[dict[str, object]], Path]:
        """Return a pydantic default factory: ``quarry_root`` joined with *parts*.

        Pydantic hands a default factory the fields validated so far, so the
        derived path follows ``QUARRY_ROOT``, and the home directory is only
        resolved when the root itself is left unset.
        """

        def make(validated: dict[str, object]) -> Path:
            return cast("Path", validated["quarry_root"]).joinpath(*parts)

        return make

    @classmethod
    def tls_dir(cls) -> Path:
        """Return the directory ``quarry install`` writes the daemon's TLS files to."""
        return cls.root() / "tls"

    @classmethod
    def daemon_ca(cls) -> Path:
        """Return the daemon's pinned CA certificate."""
        return cls.tls_dir() / "ca.crt"

    @classmethod
    def proxy_config(cls) -> Path:
        """Return the stored remote-server config."""
        return cls.home() / ".punt-labs" / "mcp-proxy" / "quarry.toml"

    @classmethod
    def proxy_ca(cls) -> Path:
        """Return the CA certificate stored with the remote-server config."""
        return cls.home() / ".punt-labs" / "mcp-proxy" / "quarry-ca.crt"

    @classmethod
    def env_file(cls) -> Path:
        """Return the service's environment file (it holds the API key)."""
        return cls.root() / "quarry.env"

    @classmethod
    def claude_projects(cls) -> Path:
        """Return Claude Code's per-project transcript directory."""
        return cls.home() / ".claude" / "projects"

    @classmethod
    def ethos_identities(cls) -> Path:
        """Return the global ethos identities directory."""
        return cls.home() / ".punt-labs" / "ethos" / "identities"

    @classmethod
    def launchd_dir(cls) -> Path:
        """Return the user's launchd agents directory (macOS)."""
        return cls.home() / "Library" / "LaunchAgents"

    @classmethod
    def launchd_plist(cls, label: str) -> Path:
        """Return the launchd plist for the service *label*."""
        return cls.launchd_dir() / f"{label}.plist"

    @classmethod
    def systemd_dir(cls) -> Path:
        """Return the user's systemd unit directory (Linux)."""
        return cls.home() / ".config" / "systemd" / "user"

    @classmethod
    def systemd_unit(cls) -> Path:
        """Return quarry's systemd user unit."""
        return cls.systemd_dir() / "quarry.service"
