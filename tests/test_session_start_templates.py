"""Tests for :class:`quarry.session_start_templates.SessionStartTemplates`.

The templates are text; a directory arrives already quoted by the reply that
builds them (see ``test_session_start_reply``).
"""

from __future__ import annotations

from quarry.session_start_templates import SessionStartTemplates

_R1 = (
    "Understanding code, a project decision, or a memory? See the quarry-recall skill."
)
_R2 = "Remembering, learning, or ingesting something? See the quarry-capture skill."
_TRAILER = f"{_R1}\n{_R2}"


class TestSubsumption:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.subsumption("'/repo/parent'")
        expected = (
            "Quarry: child registrations exist under '/repo/parent'. "
            "Auto-register skipped to prevent subsumption. "
            "To register the parent run: quarry enable '/repo/parent'\n"
            f"{_TRAILER}"
        )
        assert result == expected


class TestDaemonUnreachable:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.daemon_unreachable("'/repo/solo'")
        expected = (
            "Quarry is enabled for this repo but quarryd is currently unreachable.\n"
            "Once you restart it (systemctl --user restart quarry / "
            "launchctl kickstart) the tools below become available.\n"
            "Auto-registration of '/repo/solo' is deferred until quarryd returns.\n"
            f"{_TRAILER}"
        )
        assert result == expected


class TestNudgeEnable:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.nudge_enable("'/repo/unopted'")
        expected = (
            "Quarry semantic search is available but not enabled for this project.\n"
            "Directory: '/repo/unopted'\n"
            "This directory is not registered for sync. To turn quarry on:\n"
            "  quarry enable '/repo/unopted'\n"
            "This runs once, commits an opt-in marker, deposits the agent guide,\n"
            "and registers this directory for background sync."
        )
        assert result == expected


class TestDriftSurface:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.drift_surface("'/repo/drifted'", "drifted")
        expected = (
            "Quarry: this project has an indexed collection but no opt-in marker\n"
            "('/repo/drifted', collection 'drifted'). Two doors:\n"
            "  quarry enable '/repo/drifted'         re-adopt: marker + guide.\n"
            "  quarry deregister drifted    drop the registration (keep-data).\n"
            "Auto-register is refused (already registered); auto-deregister is\n"
            "refused (would delete indexed data on marker drift)."
        )
        assert result == expected
