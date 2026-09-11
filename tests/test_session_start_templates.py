"""Tests for :class:`quarry.session_start_templates.SessionStartTemplates`."""

from __future__ import annotations

from pathlib import Path

from quarry.results import CoverageCounts
from quarry.session_start_templates import SessionStartTemplates

_R1 = (
    "Use find before WebSearch or WebFetch for research, or before "
    "answering a why/how/what-did-we-decide question."
)
_R2 = "Prefer grep for symbol and value lookups; prefer find for meaning."
_R3 = (
    "Use remember when you learn something durable — a decision, a gotcha, "
    "a non-obvious fact, a procedure — so it survives context compaction."
)
_TRAILER = f"{_R1}\n{_R2}\n{_R3}"
_SLASH_TAIL = (
    "Slash commands: /find, /ingest, /remember, /explain, /source, /quarry. "
    "For deep research across local docs and the web, use the researcher agent."
)


def _counts(
    *, documents_indexed: int, transcripts_captured: int, memories_saved: int
) -> CoverageCounts:
    """Build a :class:`CoverageCounts` literal for a template call."""
    return {
        "documents_indexed": documents_indexed,
        "transcripts_captured": transcripts_captured,
        "memories_saved": memories_saved,
    }


class TestActive:
    def test_exact_output(self) -> None:
        counts = _counts(documents_indexed=42, transcripts_captured=7, memories_saved=3)
        result = SessionStartTemplates.active(
            Path("/repo"), "myproj", "myproj-captures", counts, "launched"
        )
        expected = (
            "Quarry semantic search is active for this project.\n"
            'Collection: "myproj" (/repo)\n'
            'Captures: "myproj-captures"\n'
            "42 documents indexed, 7 transcripts captured, 3 memories saved.\n"
            f"{_TRAILER}\n"
            "Background sync in progress.\n"
            f"{_SLASH_TAIL}"
        )
        assert result == expected


class TestActiveUnreachableCoverage:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.active_unreachable_coverage(
            Path("/repo"), "myproj", "myproj-captures", "running"
        )
        expected = (
            "Quarry semantic search is active for this project.\n"
            'Collection: "myproj" (/repo)\n'
            'Captures: "myproj-captures"\n'
            "Coverage counts unavailable "
            "(quarryd unreachable or client not authorized).\n"
            f"{_TRAILER}\n"
            "Background sync already running.\n"
            f"{_SLASH_TAIL}"
        )
        assert result == expected


class TestSubsumption:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.subsumption(Path("/repo/parent"))
        expected = (
            "Quarry: child registrations exist under /repo/parent. "
            "Auto-register skipped to prevent subsumption. "
            "Run 'quarry enable /repo/parent' to register the parent.\n"
            f"{_TRAILER}"
        )
        assert result == expected


class TestDaemonUnreachable:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.daemon_unreachable(Path("/repo/solo"))
        expected = (
            "Quarry is enabled for this repo but quarryd is currently unreachable.\n"
            "Once you restart it (systemctl --user restart quarry / "
            "launchctl kickstart) the tools below become available.\n"
            "Auto-registration of /repo/solo is deferred until quarryd returns.\n"
            f"{_TRAILER}"
        )
        assert result == expected


class TestNudgeEnable:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.nudge_enable(Path("/repo/unopted"))
        expected = (
            "Quarry semantic search is available but not enabled for this project.\n"
            "Directory: /repo/unopted\n"
            "This directory is not registered for sync. To turn quarry on:\n"
            "  quarry enable /repo/unopted\n"
            "This runs once, commits an opt-in marker, deposits the agent guide,\n"
            "and registers this directory for background sync."
        )
        assert result == expected


class TestDriftSurface:
    def test_exact_output(self) -> None:
        result = SessionStartTemplates.drift_surface(Path("/repo/drifted"), "drifted")
        expected = (
            "Quarry: this project has an indexed collection but no opt-in marker\n"
            "(/repo/drifted, collection 'drifted'). Two doors:\n"
            "  quarry enable /repo/drifted         re-adopt: marker + guide.\n"
            "  quarry deregister drifted    drop the registration (keep-data).\n"
            "Auto-register is refused (already registered); auto-deregister is\n"
            "refused (would delete indexed data on marker drift)."
        )
        assert result == expected


class TestSyncLine:
    def test_launched(self) -> None:
        result = SessionStartTemplates.active(
            Path("/r"),
            "c",
            "c-captures",
            _counts(documents_indexed=0, transcripts_captured=0, memories_saved=0),
            "launched",
        )
        assert "Background sync in progress." in result

    def test_running(self) -> None:
        result = SessionStartTemplates.active(
            Path("/r"),
            "c",
            "c-captures",
            _counts(documents_indexed=0, transcripts_captured=0, memories_saved=0),
            "running",
        )
        assert "Background sync already running." in result

    def test_unrecognized_status_falls_back_to_failed(self) -> None:
        """Any status other than launched/running renders the failure line."""
        result = SessionStartTemplates.active(
            Path("/r"),
            "c",
            "c-captures",
            _counts(documents_indexed=0, transcripts_captured=0, memories_saved=0),
            "failed",
        )
        assert "Background sync failed to launch." in result
