"""The ``quarry prune-mcp-shadow`` install-script-internal command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import typer

    from quarry.cli_captures import CliPlumbing


@final
class AdminCli:
    """Serve install-script-internal admin commands around a plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the admin commands to *app* (``prune-mcp-shadow`` is hidden)."""
        app.command(name="prune-mcp-shadow", hidden=True)(self._prune_mcp_shadow)
        app.command(name="uninstall")(self._p.cli_errors(self._uninstall))

    def _prune_mcp_shadow(self) -> None:
        """Strip a stray user-scope ``mcpServers.quarry`` shadow entry (PL-999).

        install.sh's own tail, run right after the plugin install step so a
        fresh machine never carries the shadow even one `quarry doctor` run --
        running the full doctor there would be noisy and slow for what is a
        single opportunistic file edit. Hidden: an install-script internal,
        not a command end users invoke.
        """
        self._p.emit(*self._shadow_message())

    def _uninstall(self) -> None:
        """Remove quarry system daemon and service file.

        Stops the daemon and removes the launchd plist (macOS) or systemd unit
        (Linux).  Clean inverse of the daemon step in ``quarry install``.  Also
        opportunistically prunes a stray user-scope ``mcpServers.quarry`` entry
        from ``~/.claude.json`` (PL-999) -- harmless when the plugin is absent
        or the entry is already gone.
        """
        from quarry.service import uninstall as svc_uninstall  # noqa: PLC0415

        self._shadow_message()
        msg = svc_uninstall()
        self._p.emit({"message": msg}, msg)

    @staticmethod
    def _shadow_message() -> tuple[dict[str, str], str]:
        """Prune the stray shadow entry; return its ``emit`` payload and line."""
        from quarry.doctor_mcp import ClaudeCodeMcp  # noqa: PLC0415
        from quarry.user_scope_shadow import UserScopeShadow  # noqa: PLC0415

        message = UserScopeShadow(ClaudeCodeMcp()).prune().message()
        return {"message": message}, message
