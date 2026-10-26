"""The paths quarry keeps under the home directory, resolved when asked."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast, final

if TYPE_CHECKING:
    from collections.abc import Callable


@final
class QuarryHome:
    """Name the files under ``~`` without resolving ``~`` until a path is wanted.

    A module that resolved the home directory when it was imported would fail to
    import on a host where it cannot be resolved (no ``HOME``, no passwd entry),
    and so would every command, the daemon and the hooks.  Each path here is
    built at the call, so only the code that really needs the home directory
    fails, with the one error ``Path.home()`` raises.  Tests substitute these
    methods (or ``Path.home``) rather than module constants.
    """

    __slots__ = ()

    @classmethod
    def root(cls) -> Path:
        """Return quarry's per-user directory."""
        return Path.home() / ".punt-labs" / "quarry"

    @classmethod
    def data_root(cls) -> Path:
        """Return the default data root, used when ``QUARRY_ROOT`` is unset."""
        return cls.root() / "data"

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
        return Path.home() / ".punt-labs" / "mcp-proxy" / "quarry.toml"

    @classmethod
    def proxy_ca(cls) -> Path:
        """Return the CA certificate stored with the remote-server config."""
        return Path.home() / ".punt-labs" / "mcp-proxy" / "quarry-ca.crt"

    @classmethod
    def env_file(cls) -> Path:
        """Return the service's environment file (it holds the API key)."""
        return cls.root() / "quarry.env"

    @classmethod
    def claude_projects(cls) -> Path:
        """Return Claude Code's per-project transcript directory."""
        return Path.home() / ".claude" / "projects"

    @classmethod
    def ethos_identities(cls) -> Path:
        """Return the global ethos identities directory."""
        return Path.home() / ".punt-labs" / "ethos" / "identities"

    @classmethod
    def launchd_dir(cls) -> Path:
        """Return the user's launchd agents directory (macOS)."""
        return Path.home() / "Library" / "LaunchAgents"

    @classmethod
    def launchd_plist(cls, label: str) -> Path:
        """Return the launchd plist for the service *label*."""
        return cls.launchd_dir() / f"{label}.plist"

    @classmethod
    def systemd_dir(cls) -> Path:
        """Return the user's systemd unit directory (Linux)."""
        return Path.home() / ".config" / "systemd" / "user"

    @classmethod
    def systemd_unit(cls) -> Path:
        """Return quarry's systemd user unit."""
        return cls.systemd_dir() / "quarry.service"
